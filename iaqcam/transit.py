"""
Pollution in transit: predicting the sensor before it records the rise (version 8).

Physics
-------
The sensor sits at one point. Pollution emitted at a bench reaches it later and spread
out. Each camera zone z is a source e_z(t) (persons, motion, motion variance seen there)
with its own transport kernel h_z(tau) to the sensor (non-negative, delayed by the travel
time). The sensor obeys

    dC/dt = S_arr(t) - r (C - C_b),     S_arr(t) = sum_z  sum_tau  h_z(tau) e_z(t - tau)

Discretised: the kernel has one weight per lag block (0-2, 2-5, 5-10, 10-20, 20-30 min
ago) per camera and per activity feature, all >= 0. They are learned by non-negative
ridge regression against the source the sensor must have received,
s_obs = dC/dt + r (C - C_b) (physics-supervised: no manual labels).

From the weights follow, per camera, the travel time to the sensor (weighted lag) and
the emission strength, and per activity feature an emission factor in physical units
(sensor unit x m3 per minute: ug/min for particles, mL/min for CO2).

Forecast at time t for every minute k = 1..30 ahead:
  committed   only what was emitted before t (everyone stops now): pollution in transit.
              Physically certain: it is already in the air.
  expected    plus the future activity as this lab usually continues it: each camera's
              activity relaxes from its current value to its typical level with its own
              persistence (AR(1) per camera and feature, learned from the camera minutes)
  continue    plus the current activity held unchanged (worst case)
  fan_on      'expected' with the removal rate of the exhaust fan
Two warning tiers follow: committed (no false alarms by construction, short lead) and
expected (longer lead, some false alarms when work stops earlier than usual).
The paths are integrated exactly minute by minute. Time-to-threshold = first minute a
path reaches the fan start threshold.
"""
import numpy as np

BLOCKS = ((0, 2), (2, 5), (5, 10), (10, 20), (20, 30))    # minutes ago [a, b)
FEATURES = ("persons", "motion", "variance")
HORIZON = 30


def nn_ridge(X, y, alpha=1.0, iters=300):
    """min |Xw - y|^2 + alpha |w|^2 with w >= 0 (coordinate descent; columns pre-scaled)."""
    G = X.T @ X + alpha * np.eye(X.shape[1])
    c = X.T @ y
    w = np.zeros(X.shape[1])
    diag = np.diag(G)
    for _ in range(iters):
        w_old = w.copy()
        for j in range(len(w)):
            g = c[j] - G[j] @ w + diag[j] * w[j]
            w[j] = max(0.0, g / diag[j])
        if np.max(np.abs(w - w_old)) < 1e-7 * (1 + np.max(np.abs(w))):
            break
    return w


def _cumsum(x):
    """Cumulative sums with a leading 0 (NaN counted as 0) and of the missing flags."""
    miss = np.isnan(x)
    return np.concatenate([[0.0], np.cumsum(np.where(miss, 0.0, x))]), \
        np.concatenate([[0], np.cumsum(miss)])


def block_means(cs, cm, origins, k, a, b, future_value=None, relax=None):
    """Mean of x over minutes [u-b+1, u-a] with u = origin + k. Minutes after the origin
    use future_value (array per origin; None = 0, i.e. no more emission); with relax =
    (mu, rho) the future minute j after the origin is mu + (future_value - mu) rho^j.
    Returns (mean, ok) where ok is False when a past minute of the window is missing."""
    n = len(cs) - 1
    u = origins + k
    lo, hi = u - b + 1, u - a
    past_hi = np.minimum(hi, origins)
    lo_c = np.clip(lo, 0, n)
    has_past = past_hi >= lo
    ph = np.clip(past_hi + 1, 0, n)
    s_past = np.where(has_past, cs[ph] - cs[lo_c], 0.0)
    miss = np.where(has_past, cm[ph] - cm[lo_c], 0)
    fut = np.maximum(0, hi - np.maximum(lo - 1, origins))
    if future_value is None:
        total = s_past
    elif relax is None:
        total = s_past + fut * future_value
    else:
        mu, rho = relax
        j1 = np.maximum(lo, origins + 1) - origins              # first future minute, 1-based
        geo = np.where(fut > 0, rho ** j1 * (1 - rho ** fut) / max(1e-9, 1 - rho), 0.0)
        total = s_past + fut * mu + (future_value - mu) * geo
    return total / (b - a), (miss == 0) & (lo >= 0)


class TransitModel:
    """Fitted per pollutant. zones: {camera id: array (n, 3)} of persons, motion, variance."""

    def __init__(self, alpha=3.0):
        self.alpha = alpha
        self.w = None

    # --------------------------------------------------------------- design
    def _prep(self, zones):
        self.cams = sorted(zones)
        self.series = {c: zones[c] for c in self.cams}
        self.cs = {(c, f): _cumsum(self.series[c][:, f]) for c in self.cams for f in range(len(FEATURES))}

    def design(self, origins, k=0, scenario="observed"):
        """Columns (camera, feature, block). scenario observed = the real past at origin+k
        (k = 0 for fitting); committed / expected / continue = seen from the origin."""
        cols, ok = [], np.ones(len(origins), bool)
        for c in self.cams:
            for f in range(len(FEATURES)):
                cs, cm = self.cs[(c, f)]
                fv, relax = None, None
                if scenario in ("continue", "expected"):
                    fv = np.nan_to_num(self.series[c][np.clip(origins, 0, len(self.series[c]) - 1), f])
                if scenario == "expected":
                    relax = self.persist.get((c, f), (0.0, 0.0))
                for a, b in BLOCKS:
                    if scenario == "observed":
                        m, good = block_means(cs, cm, origins + k, 0, a, b, None)
                    else:
                        m, good = block_means(cs, cm, origins, k, a, b, fv, relax)
                    cols.append(m)
                    ok &= good
        return np.column_stack(cols), ok

    # ------------------------------------------------------------------ fit
    def fit(self, zones, s_obs, rows):
        """Learn the non-negative kernels on the given minute indexes."""
        self._prep(zones)
        # how this lab's activity usually continues: AR(1) per camera and feature (training rows)
        self.persist = {}
        rows_a = np.asarray(rows)
        for c in self.cams:
            for f in range(len(FEATURES)):
                x = self.series[c][:, f]
                a_, b_ = x[rows_a[rows_a >= 1] - 1], x[rows_a[rows_a >= 1]]
                good_ = np.isfinite(a_) & np.isfinite(b_)
                mu = float(np.mean(b_[good_])) if good_.sum() else 0.0
                rho = 0.0
                if good_.sum() > 30 and np.std(a_[good_]) > 0:
                    rho = float(np.clip(np.corrcoef(a_[good_], b_[good_])[0, 1], 0.0, 0.995))
                self.persist[(c, f)] = (mu, rho)
        X, ok = self.design(rows_a)
        good = ok & np.isfinite(s_obs[rows])
        X, y = X[good], s_obs[rows][good]
        if len(y) < 120 or X.shape[1] == 0:
            self.w = None
            return self
        self.scale = np.maximum(X.std(axis=0), 1e-9)
        Z = X / self.scale
        self.mx, self.my = Z.mean(axis=0), float(y.mean())
        w = nn_ridge(Z - self.mx, y - self.my, alpha=self.alpha * len(y) / 1000.0)
        self.w = w / self.scale                                  # weights on raw block means
        self.b0 = self.my - float((self.mx * self.scale) @ self.w)
        self.n = int(len(y))
        pred = X @ self.w + self.b0
        ss = float(((y - y.mean()) ** 2).sum())
        self.r2 = 1 - float(((y - pred) ** 2).sum()) / ss if ss > 0 else 0.0
        return self

    @property
    def ready(self):
        return self.w is not None

    def source(self, origins, k, scenario):
        X, ok = self.design(origins, k, scenario)
        return X @ self.w + self.b0, ok

    # ---------------------------------------------------------------- paths
    def paths(self, origins, c0, cb, r_now, r_fan=None, horizon=HORIZON):
        """Concentration paths (n_origins, horizon) for committed / continue / fan_on."""
        out = {}
        for name, scen, r in (("committed", "committed", r_now), ("expected", "expected", r_now),
                              ("continue", "continue", r_now), ("fan_on", "expected", r_fan if r_fan else None)):
            if r is None:
                continue
            c = np.array(c0, dtype=float)
            r = np.broadcast_to(np.asarray(r, float), c.shape)
            path, valid = [], np.ones(len(c), bool)
            decay = np.exp(-r)
            gain = np.where(r > 1e-9, (1 - decay) / np.maximum(r, 1e-9), 1.0)
            for k in range(1, horizon + 1):
                s, ok = self.source(origins, k, scen)
                valid &= ok
                c = cb + (c - cb) * decay + s * gain                # exact step, S held for the minute
                path.append(c.copy())
            out[name] = np.column_stack(path)
            out[name + "_ok"] = valid
        return out

    # ------------------------------------------------------- interpretation
    def summary(self, volume, names=None):
        """Per camera: share of the source and travel time; per feature: emission factor."""
        if not self.ready:
            return None
        mids = np.array([(a + b) / 2 for a, b in BLOCKS])
        W = self.w.reshape(len(self.cams), len(FEATURES), len(BLOCKS))
        means = np.array([[np.nanmean(self.series[c][:, f]) for f in range(len(FEATURES))] for c in self.cams])
        contrib = W * np.nan_to_num(means)[:, :, None]                 # typical contribution per block
        total = contrib.sum()
        cams = []
        for i, c in enumerate(self.cams):
            ci = contrib[i].sum(axis=0)                                  # per block
            delay = float((ci * mids).sum() / ci.sum()) if ci.sum() > 0 else None
            cams.append({"camera": c, "name": (names or {}).get(c, f"C{c}"),
                         "share": float(ci.sum() / total) if total > 0 else 0.0,
                         "travel_min": None if delay is None else round(delay, 1),
                         "kernel": [round(float(v), 6) for v in W[i].sum(axis=0)]})
        ef = {FEATURES[f]: float(W[:, f, :].sum() / max(1, len(self.cams))) for f in range(len(FEATURES))}
        return {"cameras": cams, "r2": round(self.r2, 3), "n": self.n,
                "emission_per_min": {k: round(v, 5) for k, v in ef.items()},          # sensor unit per minute
                "emission_mass_per_min": {k: round(v * volume, 3) for k, v in ef.items()},  # x m3
                "baseline": round(self.b0, 5)}


def time_to(path, threshold):
    """First minute (1-based) where the path reaches the threshold, else None."""
    hit = np.flatnonzero(path >= threshold)
    return int(hit[0]) + 1 if len(hit) else None


def lead_time_eval(y, origins, ttt, start, stop, confirm=2, expire=40):
    """Replays the early-warning rule of the app over consecutive minutes.
    ttt: predicted minutes to the threshold per origin (None/NaN = not within the horizon).
    Returns {'confirmed': [lead minutes...], 'missed': n, 'false_alarms': n, 'crossings': n}."""
    out = {"confirmed": [], "missed": 0, "false_alarms": 0, "crossings": 0}
    crossed, active, seen = False, None, 0
    for t, tt in zip(origins, ttt):
        v = y[t]
        if not np.isfinite(v):
            continue
        has = tt is not None and np.isfinite(tt)
        if v >= start:
            if not crossed:
                out["crossings"] += 1
                if active is not None:
                    out["confirmed"].append(float(t - active))
                else:
                    out["missed"] += 1
                crossed, active, seen = True, None, 0
            continue
        if v < stop:
            crossed = False
        if crossed:
            continue
        seen = seen + 1 if has else 0
        if active is None and seen >= confirm:
            active = t
        elif active is not None and t - active > expire and not has:
            out["false_alarms"] += 1
            active = None
    return out


def ttt_from_paths(paths, threshold):
    """Minutes to the threshold per row of a (n, horizon) path array (NaN = not reached)."""
    hit = paths >= threshold
    first = np.where(hit.any(axis=1), hit.argmax(axis=1) + 1, np.nan)
    return first
