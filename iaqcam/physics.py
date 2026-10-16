"""
Physics of the room air: single-zone mass balance (EAMTP research plan, sections 5.1-5.2).

    dC/dt = S_t - r_t (C - C_b)

    C     concentration of a pollutant in the room (sensor value)
    C_b   background the room air relaxes to (outdoor air, or the empty-room level when
          no outdoor sensor exists: estimated from the lowest values of the history)
    r_t   removal rate per minute = air-change rate lambda (+ deposition k_dep for
          particles). For CO2 r = lambda.
    S_t   source term per minute = (E_mach + E_res) / V: what people, machines and
          activity add; for CO2 S = N_t G / V.

Everything is per minute because the database holds one row per minute.

What is estimated here, all from data the app already stores:

1. Background C_b (per pollutant): a low percentile of the history (floor 400 ppm for CO2).
2. Removal rate r from decay periods: stretches where nobody is in the areas and the value
   falls towards the background. ln(C - C_b) falls on a straight line with slope -r
   (tracer-gas decay method). Fitted separately for "fan on" and "fan off" minutes when the
   fan is really switched (not in dry run), so a new ventilation state changes r, not
   the learned weights.
3. Online lambda_t for CO2 while people are present (plan 5.1): a rolling least-squares fit
   of dC/dt = a N_t + b M_t - lambda_t (C - C_b) over the last hour, with N_t the persons
   and M_t the total motion seen by the cameras. a * V gives the CO2 generation per person,
   a physical check of the camera counts (about 0.3 L/min for seated adults).
4. The source S_t as a function of what the cameras see: the observed source
   s_t = dC/dt + r (C - C_b) is regressed on the camera drivers (with their delays).
5. The physics forecast Phi_H: the mass balance integrated H minutes ahead with the current
   source held constant (closed form of the linear ODE).
"""
import math

import numpy as np

PPM_KINDS = ("co2",)


# ------------------------------------------------------------------ helpers
def derivative(y):
    """Centred difference per minute (one-sided at the ends), NaN where unknown."""
    d = np.full_like(y, np.nan)
    if len(y) >= 3:
        d[1:-1] = (y[2:] - y[:-2]) / 2.0
    if len(y) >= 2:
        d[0] = y[1] - y[0]
        d[-1] = y[-1] - y[-2]
    return d


def smooth(y, k=3):
    """Centred moving average over k minutes (NaN-aware, keeps NaN where all are NaN)."""
    if k <= 1:
        return y.copy()
    out = np.full_like(y, np.nan)
    h = k // 2
    for i in range(len(y)):
        w = y[max(0, i - h):i + h + 1]
        w = w[~np.isnan(w)]
        if len(w):
            out[i] = w.mean()
    return out


def background(y, kind, quantile=2.0):
    """Level the room air relaxes to when nothing emits."""
    v = y[~np.isnan(y)]
    if len(v) < 30:
        return None
    b = float(np.percentile(v, quantile))
    if kind == "co2":
        b = max(b, 400.0)
    return b


# ------------------------------------------------------- removal rate (decay)
def decay_segments(y, persons, cb, min_len=12, min_excess=None):
    """Index ranges [i, j) where the room is empty (persons == 0 for the whole range and the
    5 minutes before) and the value falls towards the background."""
    n = len(y)
    if cb is None:
        return []
    excess = y - cb
    floor = min_excess if min_excess is not None else max(1e-9, 0.05 * np.nanpercentile(np.abs(excess), 90))
    empty = np.zeros(n, bool)
    for i in range(n):
        lo = max(0, i - 5)
        p = persons[lo:i + 1]
        empty[i] = len(p) and not np.isnan(p).any() and np.all(p == 0)
    segs, i = [], 0
    while i < n:
        if not (empty[i] and excess[i] > floor and not np.isnan(y[i])):
            i += 1
            continue
        j = i
        while j < n and empty[j] and not np.isnan(y[j]) and excess[j] > floor:
            j += 1
        if j - i >= min_len:
            segs.append((i, j))
        i = j
    return segs


def fit_decay(y, cb, seg):
    """(rate per minute, R^2) of ln(C - C_b) = a - r t over one segment, or None."""
    i, j = seg
    e = y[i:j] - cb
    if np.any(e <= 0):
        return None
    t = np.arange(j - i, dtype=float)
    ly = np.log(e)
    A = np.column_stack([np.ones_like(t), t])
    coef, *_ = np.linalg.lstsq(A, ly, rcond=None)
    pred = A @ coef
    ss = float(((ly - ly.mean()) ** 2).sum())
    r2 = 1.0 - float(((ly - pred) ** 2).sum()) / ss if ss > 0 else 0.0
    rate = -float(coef[1])
    if rate <= 0 or r2 < 0.6:
        return None
    return rate, r2


def removal_rate(y, persons, cb, fan=None, default=None):
    """Removal rate per minute from all decay periods, split by fan state when known.
    Returns {'off': r, 'on': r or None, 'n_off': k, 'n_on': k, 'source': text}."""
    out = {"off": None, "on": None, "n_off": 0, "n_on": 0, "source": "default"}
    rates = {"off": [], "on": []}
    for seg in decay_segments(y, persons, cb):
        fit = fit_decay(y, cb, seg)
        if fit is None:
            continue
        state = "off"
        if fan is not None:
            f = fan[seg[0]:seg[1]]
            if np.all(f == 1):
                state = "on"
            elif not np.all(f == 0):
                continue                              # fan switched during the decay: ambiguous
        rates[state].append(fit[0])
    for state in ("off", "on"):
        if rates[state]:
            out[state] = float(np.median(rates[state]))
            out["n_" + state] = len(rates[state])
    if out["off"] is not None or out["on"] is not None:
        out["source"] = "decay"
    if out["off"] is None:
        out["off"] = default if default is not None else out["on"]
        if out["source"] == "decay":
            out["source"] = "decay (fan on) + default"
    return out


def rate_series(rates, fan, n):
    """Removal rate per minute given the fan state per minute."""
    r = np.full(n, rates["off"] if rates["off"] is not None else np.nan)
    if fan is not None and rates.get("on") is not None:
        r[fan == 1] = rates["on"]
    return r


# ------------------------------------------------- online lambda (CO2, occupied)
def online_lambda(y, cb, persons, motion=None, window=60, min_rows=30):
    """
    Rolling least squares of dC/dt = a N + b M - lambda (C - C_b) over the last `window`
    minutes. Returns (lambda_t per minute, a_t, b_t): NaN where the fit is not possible
    (too few rows, nobody present, or a non-physical result).
    """
    n = len(y)
    lam, a_out, b_out = (np.full(n, np.nan) for _ in range(3))
    if cb is None:
        return lam, a_out, b_out
    dy = derivative(smooth(y, 3))
    ex = y - cb
    M = motion if motion is not None else np.zeros(n)
    for t in range(window, n):
        sl = slice(t - window, t + 1)
        rows = ~(np.isnan(dy[sl]) | np.isnan(ex[sl]) | np.isnan(persons[sl]) | np.isnan(M[sl]))
        if rows.sum() < min_rows or np.nansum(persons[sl][rows]) == 0:
            continue
        cols = [persons[sl][rows], -ex[sl][rows]]
        use_m = np.nanstd(M[sl][rows]) > 0
        if use_m:
            cols.insert(1, M[sl][rows])
        A = np.column_stack(cols)
        if np.linalg.cond(A) > 1e6:
            continue
        coef, *_ = np.linalg.lstsq(A, dy[sl][rows], rcond=None)
        lam_t = coef[-1]
        if lam_t <= 0 or coef[0] < 0 or lam_t > 0.5:          # > 30 air changes per hour: not physical here
            continue
        lam[t], a_out[t] = lam_t, coef[0]
        b_out[t] = coef[1] if use_m else 0.0
    return lam, a_out, b_out


def co2_generation_lpm(a_ppm_per_min, volume_m3):
    """Per-person CO2 generation (litres per minute) from the fitted a = G / V in ppm/min."""
    if a_ppm_per_min is None or volume_m3 is None or not math.isfinite(a_ppm_per_min):
        return None
    return float(a_ppm_per_min * 1e-6 * volume_m3 * 1000.0)


# ----------------------------------------------------------- source and prior
def observed_source(y, cb, r):
    """s_t = dC/dt + r_t (C - C_b): what must have been emitted per minute."""
    return derivative(smooth(y, 3)) + r * (y - cb)


def phi(c0, cb, r, s, h):
    """Mass balance integrated h minutes ahead with constant source s and rate r
    (vectorised). r -> 0 falls back to linear growth."""
    c0, r, s = np.asarray(c0, float), np.asarray(r, float), np.asarray(s, float)
    out = np.full(np.broadcast(c0, r, s).shape, np.nan)
    small = np.abs(r) < 1e-6
    big = ~small
    with np.errstate(invalid="ignore", over="ignore"):
        steady = cb + np.divide(s, r, out=np.zeros_like(out), where=big)
        out = np.where(big, steady + (c0 - steady) * np.exp(-r * h), c0 + s * h)
    return out
