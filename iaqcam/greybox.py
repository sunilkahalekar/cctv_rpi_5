"""
Physics-informed and grey-box forecasts of one pollutant (EAMTP plan, section 5.2).

Target: the change of the sensor value over the lead time H (10 min),
dC = C(t+H) - C(t). Five forecasts are compared on the same test minutes:

  persistence  dC = 0 ("no change"), the floor every model must beat
  physics      mass balance only: dC_phys = Phi_H(C_t; r_t, C_b, s_t) - C_t (physics.py).
               The source s_t = (E_res + E_mach)/V comes from an emission model: the
               source each minute must have had (dC/dt + r (C - C_b)) regressed on what the
               cameras saw in the past 30 minutes (persons, motion, with their delays) -
               for CO2 that is N_t G / V, i.e. the per-person generation. Without camera
               data the source of the last 10 minutes is held.
  data         ridge regression on the sensor's own history and the camera drivers
               (the forecast of version 6)
  greybox      dC_phys + ridge regression of the RESIDUAL dC - dC_phys on the recent changes
               (not the level), the camera drivers and the context (fan state, removal rate,
               door traffic): physics carries
               dilution and removal, the data part learns only what physics misses
               (Tian et al. 2024 pattern; plan variant b)
  physreg      ridge regression of dC whose loss also penalises disagreement with the mass
               balance:  |y - Zw|^2 + mu |dC_phys - Zw|^2 + alpha |w|^2
               (physics-informed loss in closed form, Kim et al. 2026 loss design;
               plan variant c)

The test minutes are the last quarter of the history, separated from the training
minutes by an embargo gap (input window + lead), so overlapping windows cannot leak.
The variant with the lowest error that also beats "no change" is used.
"""
import numpy as np

from . import physics as P

VARIANTS = ("physics", "data", "greybox", "physreg")
LIMIT = 6.0          # standardised inputs are kept within +-6 training standard deviations


class Ridge:
    """Ridge regression on standardised inputs; optional physics penalty (mu, prior)."""

    def __init__(self, alpha=1.0, mu=0.0):
        self.alpha, self.mu = alpha, mu

    def fit(self, X, y, prior=None):
        self.mean, self.std = X.mean(axis=0), X.std(axis=0)
        # an input (almost) constant in training, e.g. the fan never on, must not explode
        # when it changes later: floor the scale and keep standardised inputs in range
        self.std = np.maximum(self.std, 1e-3 * np.maximum(1.0, np.abs(self.mean)))
        Z = (X - self.mean) / self.std
        target = y if prior is None or self.mu == 0 else (y + self.mu * prior) / (1.0 + self.mu)
        self.y0 = float(target.mean())
        gram = (1.0 + (self.mu if prior is not None else 0.0)) * (Z.T @ Z)
        rhs = Z.T @ ((y - self.y0) + (self.mu * (prior - self.y0) if prior is not None and self.mu else 0.0))
        self.w = np.linalg.solve(gram + self.alpha * np.eye(Z.shape[1]), rhs)
        return self

    def predict(self, X):
        return np.clip((X - self.mean) / self.std, -LIMIT, LIMIT) @ self.w + self.y0


def physics_inputs(y, kind, persons, motion, fan, door, default_rate, volume, extra=None):
    """Everything the mass balance needs, per minute. Returns a dict (see keys)."""
    n = len(y)
    ys = P.smooth(y, 5)
    cb = P.background(ys, kind)
    if cb is None:
        return None
    pers = np.where(np.isnan(persons), np.nan, persons) if persons is not None else np.full(n, np.nan)
    rates = P.removal_rate(y, pers, cb, fan, default=default_rate)
    if rates["off"] is None:
        return None
    r = P.rate_series(rates, fan, n)
    lam_online = np.full(n, np.nan)
    gen = None
    if kind == "co2" and persons is not None:
        lam_online, a, _ = P.online_lambda(y, cb, pers, motion)
        if np.isfinite(lam_online).sum() >= 30:
            gen = P.co2_generation_lpm(float(np.nanmedian(a)), volume)
            # where a live estimate exists it follows windows / doors the decay fit cannot see
            r = np.where(np.isfinite(lam_online), 0.5 * r + 0.5 * lam_online, r)
    # source of the last 10 minutes (only past values: usable at the forecast origin)
    k = 10
    s_recent = np.full(n, np.nan)
    if n > k:
        mean_k = np.convolve(np.nan_to_num(y), np.ones(k) / k, mode="full")[:n]
        s_recent[k:] = (y[k:] - y[:-k]) / k + r[k:] * (mean_k[k:] - cb)
        lo, hi = np.nanpercentile(s_recent, [1, 99]) if np.isfinite(s_recent).sum() > 50 else (-np.inf, np.inf)
        s_recent = np.clip(s_recent, lo, hi)
    ctx_cols = [fan if fan is not None else np.zeros(n), r * 60.0]
    if door is not None:
        ctx_cols.append(door)
    for col in extra or []:            # e.g. machines running / enclosures open (E_mach)
        ctx_cols.append(col)
    return {"cb": cb, "rates": rates, "r": r, "lambda_online": lam_online, "co2_gen_lpm": gen,
            "s_recent": s_recent, "s_obs": P.observed_source(y, cb, r), "ctx": np.column_stack(ctx_cols)}


def physics_delta(y, phys, lead, source=None):
    s = phys["s_recent"] if source is None else source
    return P.phi(y, phys["cb"], phys["r"], s, lead) - y


class Emission:
    """Source per minute from the camera drivers (linear, fitted on training minutes)."""

    def fit(self, Xd, s_obs, rows):
        good = rows[~np.isnan(s_obs[rows]) & ~np.isnan(Xd[rows]).any(axis=1)]
        self.model = Ridge(alpha=3.0).fit(Xd[good], s_obs[good]) if len(good) >= 60 else None
        return self

    def source(self, Xd):
        if self.model is None:
            return None
        s = np.full(len(Xd), np.nan)
        ok = ~np.isnan(Xd).any(axis=1)
        s[ok] = self.model.predict(Xd[ok])
        return s


def compare(y, Xs, Xd, phys, lead, min_samples, window=30, mu=1.0):
    """Fit and test every variant. Returns a dict with the errors, the chosen variant and
    the models refitted on all usable minutes (for the live forecast)."""
    n = len(y)
    target = np.full(n, np.nan)
    target[:-lead] = y[lead:] - y[:-lead]
    Z_data = Xs if Xd is None else np.column_stack([Xs, Xd])
    # the physics part sees the level C_t; the learned part gets only changes, the camera
    # drivers and the context - an absolute level is specific to one room and condition and
    # makes the learned part fight the physics when the ventilation changes (plan 5.4)
    Z_grey = None if phys is None else np.column_stack([Xs[:, 1:]] + ([Xd] if Xd is not None else []) + [phys["ctx"]])
    if phys is None:
        return {"n": 0, "used": "no change", "mae": {}, "models": {}}
    base = physics_delta(y, phys, lead)             # source held: defined wherever y is
    ok = ~np.isnan(target) & ~np.isnan(Z_grey).any(axis=1) & ~np.isnan(base)
    idx = np.flatnonzero(ok)
    out = {"n": len(idx), "used": "no change", "mae": {}, "models": {}}
    if len(idx) < min_samples:
        return out
    seen = target[idx]
    out["limits"] = (float(np.percentile(seen, 0.5)), float(np.percentile(seen, 99.5)))
    cut = int(len(idx) * 0.75)
    gap = window + lead                                  # embargo: overlapping windows never leak
    test = idx[cut:]
    train = idx[:cut][idx[:cut] < idx[cut] - gap] if cut < len(idx) else idx[:cut]
    if len(train) < min_samples // 2 or len(test) < 20:
        return out

    def physics_for(rows):
        """dC of the mass balance with the emission model fitted on `rows` (or the held source)."""
        if Xd is None:
            return base, None
        em = Emission().fit(Xd, phys["s_obs"], rows)
        src = em.source(Xd)
        if src is None:
            return base, None
        src = np.where(np.isnan(src), phys["s_recent"], src)
        return physics_delta(y, phys, lead, src), em

    dphys, _ = physics_for(train)
    def fit(name, rows):
        if name == "data":
            return Ridge().fit(Z_data[rows], target[rows])
        if name == "greybox":
            return Ridge(alpha=3.0).fit(Z_grey[rows], target[rows] - dphys[rows])
        if name == "physreg":
            return Ridge(mu=mu).fit(Z_grey[rows], target[rows], prior=dphys[rows])
        return None

    def predict(name, model, rows):
        if name == "physics":
            return dphys[rows]
        if name == "data":
            return model.predict(Z_data[rows])
        if name == "greybox":
            return dphys[rows] + model.predict(Z_grey[rows])
        return model.predict(Z_grey[rows])

    out["mae"]["persistence"] = float(np.mean(np.abs(target[test])))
    for name in VARIANTS:
        model = fit(name, train)
        out["mae"][name] = float(np.mean(np.abs(predict(name, model, test) - target[test])))
    best = min(VARIANTS, key=lambda v: out["mae"][v])
    if out["mae"][best] < out["mae"]["persistence"]:
        out["used"] = best
    # final models on every usable minute (emission model too)
    dphys, em = physics_for(idx)
    for name in VARIANTS:
        out["models"][name] = fit(name, idx)
    out["predict"] = lambda name, rows: predict(name, out["models"][name], rows)
    out["Z_ok"] = lambda i: not (np.isnan(Z_grey[i]).any() or np.isnan(dphys[i]))
    out["dphys"] = dphys
    out["emission"] = em
    return out
