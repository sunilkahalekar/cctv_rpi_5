"""
Delay-aware forecast of the sensor values, 10 minutes ahead.

The idea
--------
People and their motion release or stir up pollution, but the air needs time to
carry it to the sensor. That travel time is what makes a forecast possible: what
was seen by the cameras during the last half hour is partly still "on its way".

Three plain steps, no neural network:

1. Travel time.  For each sensor and each driver (persons, mean motion, motion
   variance) the correlation between the driver and the later change of the
   sensor value is computed for delays of 0..30 minutes. The delay with the
   highest correlation is the estimated travel time.

2. Delayed response.  The change of the sensor value over the next 10 minutes is
   written as a weighted sum of
     - the sensor's own state (level, recent slope), and
     - each driver averaged over blocks of the past 30 minutes
       (0-5, 5-10, 10-15, 15-20, 20-30 minutes ago).
   The weights are found by least squares on the stored minutes. Pollution that
   is still travelling shows up in the older blocks.

3. Learning from its own misses.  Every forecast is compared with what the sensor
   really showed 10 minutes later. The recent average miss is taken off the next
   forecast, and the live error is reported next to the error of simply assuming
   "no change". While the model's recent forecasts are worse than "no change", it
   is paused and "no change" is shown instead; it comes back by itself once its
   forecasts are better again.

The model is refitted every minute on the stored history, so it follows the lab.

4. Physics of the room air (version 7.2, greybox.py / physics.py). The room is also
   written as a mass balance dC/dt = S - r (C - C_b): the removal rate r (air changes,
   plus deposition for particles) is estimated from decay periods of the empty room and,
   for CO2, live from the camera head count; the source S from what the cameras saw.
   Four forecasts are then compared on the same test minutes with an embargo gap:
   physics only, data only (step 2), grey-box (physics + data residual) and a regression
   with a physics-informed loss. The best one that beats "no change" is used.
"""
import threading
from datetime import datetime, timedelta

import numpy as np

from . import greybox
from .physics_events import EarlyWarnings, PhysicsEvents
from .transit import HORIZON, TransitModel, time_to
import time as _time
from .sensor_meta import describe
from .db import FIELDS, read_connection
from .sensor_meta import canonical

FMT = "%Y-%m-%d %H:%M:%S"
DRIVERS = ("persons", "total motion", "motion variance", "speed")
BLOCKS = ((0, 5), (5, 10), (10, 15), (15, 20), (20, 30))     # minutes ago: [from, to)
MAX_LAG = 30


# ------------------------------------------------------------------ helpers
def fill_small_gaps(values, max_gap):
    """Linear interpolation over runs of NaN no longer than max_gap samples."""
    v = np.asarray(values, dtype=float).copy()
    idx = np.flatnonzero(~np.isnan(v))
    for left, right in zip(idx[:-1], idx[1:]):
        gap = right - left - 1
        if 0 < gap <= max_gap:
            v[left + 1:right] = np.interp(np.arange(left + 1, right), [left, right],
                                          [v[left], v[right]])
    return v


def carry_forward(values, max_steps):
    """Repeat the last known value over up to max_steps missing samples at the end.
    The newest camera minute is only stored when it has ended, so the current minute
    would otherwise have no persons and motion."""
    v = values
    known = np.flatnonzero(~np.isnan(v))
    if len(known):
        last = known[-1]
        gap = len(v) - 1 - last
        if 0 < gap <= max_steps:
            v[last + 1:] = v[last]
    return v


def lag(values, k):
    """values shifted k minutes into the past: out[t] = values[t - k]."""
    if k == 0:
        return values.copy()
    out = np.full_like(values, np.nan)
    out[k:] = values[:-k]
    return out


def rolling_mean(values, window):
    """Mean of the last `window` samples (NaN when any of them is missing)."""
    out = np.full_like(values, np.nan)
    if len(values) >= window:
        csum = np.cumsum(np.insert(values, 0, 0.0))
        out[window - 1:] = (csum[window:] - csum[:-window]) / window
    return out


def sensor_features(y):
    return np.column_stack([y, y - lag(y, 1), y - lag(y, 5), y - lag(y, 10)])


def driver_features(drivers):
    """Block means of every driver over the past 30 minutes (see BLOCKS)."""
    cols = []
    for d in drivers:
        for start, end in BLOCKS:
            cols.append(lag(rolling_mean(d, end - start), start))
    return np.column_stack(cols)


def shift(values, k):
    """out[t] = values[t - k]; k may be negative (values from later minutes)."""
    out = np.full_like(values, np.nan)
    if k == 0:
        return values.copy()
    if k > 0:
        out[k:] = values[:-k]
    else:
        out[:k] = values[-k:]
    return out


def travel_time(driver, y):
    """
    Estimated delay (minutes) between the driver and the sensor's response.

    The sensor's change over 5 minutes is described as "decay towards a base level
    plus a push from the driver tau minutes earlier". The delay tau (0..30) that
    explains the change best is returned together with the partial correlation of
    the driver at that delay. (None, None) when the driver explains nothing.
    Taking the decay into account matters: without it, slowly varying activity
    correlates with everything and the delay cannot be told apart.
    """
    n = len(y)
    if n < 200:
        return None, None
    change = np.full_like(y, np.nan)
    change[:-5] = y[5:] - y[:-5]                         # y(t+5) - y(t)
    mean5 = rolling_mean(driver, 5)                      # mean of driver over [t-4, t]
    base_ok = ~np.isnan(change) & ~np.isnan(y)
    best = (None, None)
    for tau in range(0, MAX_LAG + 1):
        x = shift(mean5, tau - 4)                        # mean of driver over [t-tau, t-tau+4]
        ok = base_ok & ~np.isnan(x)
        if ok.sum() < 120 or np.std(x[ok]) == 0:
            continue
        A0 = np.column_stack([np.ones(ok.sum()), y[ok]])
        A1 = np.column_stack([A0, x[ok]])
        res0 = change[ok] - A0 @ np.linalg.lstsq(A0, change[ok], rcond=None)[0]
        coef = np.linalg.lstsq(A1, change[ok], rcond=None)[0]
        res1 = change[ok] - A1 @ coef
        sse0, sse1 = float(res0 @ res0), float(res1 @ res1)
        if coef[2] <= 0 or sse0 <= 0:                    # the driver must push the value up
            continue
        r = float(np.sqrt(max(0.0, 1.0 - sse1 / sse0)))
        if best[1] is None or r > best[1]:
            best = (tau, r)
    if best[1] is None or best[1] < 0.1:
        return None, None
    return best


class Ridge:
    def __init__(self, alpha=1.0):
        self.alpha = alpha

    def fit(self, X, y):
        self.mean, self.std = X.mean(axis=0), X.std(axis=0)
        self.std[self.std == 0] = 1.0
        Z = (X - self.mean) / self.std
        self.y0 = y.mean()
        self.w = np.linalg.solve(Z.T @ Z + self.alpha * np.eye(Z.shape[1]), Z.T @ (y - self.y0))
        return self

    def predict(self, X):
        return ((X - self.mean) / self.std) @ self.w + self.y0


def evaluate_and_fit(y, Xs, Xd, lead, min_samples):
    """
    y: sensor value per minute; Xs: its own features; Xd: driver features (or None).
    Trains on the first three quarters of the usable minutes and tests on the rest.
    """
    target = np.full_like(y, np.nan)
    target[:-lead] = y[lead:] - y[:-lead]                     # change over the lead time
    ok_s = ~np.isnan(target) & ~np.isnan(Xs).any(axis=1)
    out = {"n": int(ok_s.sum()), "mae_persistence": None, "mae_sensors": None, "mae_full": None,
           "used": None, "model": None, "model_full": None, "n_full": 0}
    if out["n"] < min_samples:
        return out

    # A forecast never moves further than the 10-minute changes seen in the history.
    seen = target[ok_s]
    out["limits"] = (float(np.percentile(seen, 0.5)), float(np.percentile(seen, 99.5)))

    def split_fit(X, ok):
        idx = np.flatnonzero(ok)
        cut = int(len(idx) * 0.75)
        train, test = idx[:cut], idx[cut:]
        model = Ridge().fit(X[train], target[train])
        mae = float(np.mean(np.abs(model.predict(X[test]) - target[test])))
        mae_naive = float(np.mean(np.abs(target[test])))
        return mae, mae_naive, Ridge().fit(X[idx], target[idx])

    mae_s, mae_naive, model_s = split_fit(Xs, ok_s)
    out.update(mae_persistence=mae_naive, mae_sensors=mae_s, used="sensors", model=model_s)

    if Xd is not None:
        ok_f = ok_s & ~np.isnan(Xd).any(axis=1)
        out["n_full"] = int(ok_f.sum())
        if out["n_full"] >= min_samples:
            # both models are compared on the same minutes, so the comparison is fair
            mae_f, mae_naive_same, model_f = split_fit(np.column_stack([Xs, Xd]), ok_f)
            mae_s_same, _, _ = split_fit(Xs, ok_f)
            out.update(mae_full=mae_f, mae_sensors=mae_s_same, mae_persistence=mae_naive_same,
                       model_full=model_f, n=out["n_full"])
            if mae_f < mae_s_same:
                out["used"] = "full"
    # a model that cannot beat "no change" on the test minutes is not used
    best_model_error = out["mae_full"] if out["used"] == "full" else out["mae_sensors"]
    if best_model_error >= out["mae_persistence"]:
        out["used"] = "no change"
    return out


def _r(v, d=3):
    return None if v is None or not np.isfinite(v) else round(float(v), d)


def machine_series(rows, index, n):
    """Machines running / enclosures open per minute from machine_minute rows (state held
    until the next report, at most 12 hours). None without any machine data."""
    if not rows:
        return None
    states, last = {}, {}
    out = np.full(n, np.nan)
    by_minute = {}
    for r in rows:
        i = index(r["window_start"])
        if 0 <= i < n:
            by_minute.setdefault(i, []).append((r["machine"], r["on_state"]))
    first = min(by_minute) if by_minute else n
    for i in range(first, n):
        for machine, on in by_minute.get(i, []):
            states[machine], last[machine] = (on or 0), i
        live = [v for m, v in states.items() if i - last[m] <= 720]
        out[i] = float(sum(live)) if live else np.nan
    return out


# ---------------------------------------------------------------- the thread
class Forecaster(threading.Thread):
    def __init__(self, cfg, db, state):
        super().__init__(daemon=True, name="forecast")
        f = cfg["forecast"]
        self.lead = int(f["lead_minutes"])
        self.min_samples = int(f["min_samples"])
        self.history_days = int(f["history_days"])
        self.wanted = {canonical(n) for n in f["fields"]}
        self.zone = cfg.get("sensor_zone")
        ph = cfg.get("physics") or {}
        self.physics_on = bool(ph.get("enabled", True))
        self.volume = float(ph.get("volume_m3") or 300.0)
        self.default_rate = float(ph.get("default_ach", 1.0)) / 60.0
        self.mu = float(ph.get("physics_weight", 1.0))
        limits = (((cfg.get("fan") or {}).get("ai") or {}).get("pollutants")) or {}
        self.limits = {k: {"start": float(v["start"]), "stop": float(v["stop"])}
                       for k, v in limits.items() if isinstance(v, dict) and "start" in v}
        self.events = PhysicsEvents(db, self.limits)
        self.warnings = EarlyWarnings(db)
        # pollution in transit: every camera of an inside zone is a source with its own travel time
        self.inside_cams = sorted({c for z in (cfg.get("zones") or []) if z.get("inside") for c in z.get("cameras", [])})
        self.cam_names = {c["id"]: (c.get("location") or c.get("name") or f"C{c['id']}")
                          for c in (cfg.get("cameras") or [])}
        self._tcache = {}
        self.db, self.db_path, self.state = db, cfg["database"]["path"], state
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        self._stop_event.wait(20)                    # let the first sensor poll finish
        while not self._stop_event.is_set():
            try:
                self.step()
            except Exception as exc:                 # never take the app down
                self.state.update_forecast(status=f"Forecast failed: {type(exc).__name__}: {exc}"[:200])
                print(f"[forecast] {type(exc).__name__}: {exc}")
                self.db.write_event("forecast", f"forecast failed: {type(exc).__name__}: {exc}"[:300], level="warning")
            self._stop_event.wait(60)

    # ------------------------------------------------------------------
    def _load(self):
        since = (datetime.now() - timedelta(days=self.history_days)).strftime(FMT)
        recent = (datetime.now() - timedelta(minutes=90)).strftime(FMT)
        con = read_connection(self.db_path)
        try:
            names = dict(con.execute("SELECT field, name FROM sensor_fields").fetchall())
            sensors = con.execute("SELECT window_start, " + ", ".join(FIELDS) +
                                  " FROM sensor_minute WHERE window_start >= ? ORDER BY window_start",
                                  (since,)).fetchall()
            source = ("zone_minute WHERE zone = ? AND", (self.zone, since)) if self.zone \
                else ("inside_minute WHERE", (since,))
            people = con.execute("SELECT window_start, n_person_mean, motion_total, motion_var, mu_speed FROM "
                                 + source[0] + " window_start >= ?", source[1]).fetchall()
            issued = con.execute("SELECT predicted_for, field, model_value, base_value FROM forecasts "
                                 "WHERE predicted_for >= ? ORDER BY predicted_for", (recent,)).fetchall()
            context = con.execute("SELECT window_start, fan_on, dry_run FROM context_minute "
                                  "WHERE window_start >= ?", (since,)).fetchall()
            door = con.execute("SELECT window_start, n_in, n_out FROM door_minute WHERE window_start >= ?",
                               (since,)).fetchall()
            machines = con.execute("SELECT window_start, machine, on_state FROM machine_minute "
                                   "WHERE window_start >= ? ORDER BY window_start", (since,)).fetchall()
            cams = []
            if self.inside_cams:
                cams = con.execute("SELECT window_start, camera, n_person_mean, motion_total, motion_total_var "
                                   f"FROM camera_minute WHERE window_start >= ? AND camera IN "
                                   f"({', '.join('?' * len(self.inside_cams))})", (since, *self.inside_cams)).fetchall()
        finally:
            con.close()
        self._cam_rows = cams
        return names, sensors, people, issued, context, door, machines

    def _transit(self, f, y, phys, zones):
        """Fit (every 10 minutes) and apply the pollution-in-transit model for one pollutant.
        Test: last quarter of the minutes, embargo 40 min, error at the forecast lead."""
        n, lead = len(y), self.lead
        cams = tuple(sorted(zones))
        cache = self._tcache.get(f)
        if cache is None or cache["cams"] != cams or _time.time() - cache["at"] > 600:
            idx = np.flatnonzero(np.isfinite(y[:n - lead]) & np.isfinite(y[lead:]))
            if len(idx) < self.min_samples:
                return None
            cut = int(len(idx) * 0.75)
            train = idx[:cut][idx[:cut] < idx[cut] - (30 + lead)]
            test = idx[cut:]
            m = TransitModel().fit(zones, phys["s_obs"], train)
            if not m.ready:
                return None
            p = m.paths(test, y[test], phys["cb"], phys["r"][test], horizon=lead)
            ok = p["expected_ok"]
            mae = mae_p = None
            if ok.sum() >= 20:
                tgt = y[test + lead]
                mae = float(np.mean(np.abs(p["expected"][ok, lead - 1] - tgt[ok])))
                mae_p = float(np.mean(np.abs(y[test][ok] - tgt[ok])))
            model = TransitModel().fit(zones, phys["s_obs"], idx)
            if not model.ready:
                return None
            cache = {"at": _time.time(), "cams": cams, "model": model, "mae": mae, "mae_persistence": mae_p}
            self._tcache[f] = cache
        model = cache["model"]
        model._prep(zones)                                     # newest camera minutes, same weights
        last = n - 1
        rates = phys["rates"]
        r_fan = rates.get("on") if rates.get("on") else None
        p = model.paths(np.array([last]), np.array([y[last]]), phys["cb"], np.array([phys["r"][last]]),
                        r_fan=None if r_fan is None else np.array([r_fan]), horizon=HORIZON)
        now = None
        if p["expected_ok"][0]:
            now = {k: [float(v) for v in p[k][0]] for k in ("committed", "expected", "continue", "fan_on") if k in p}
            # what each camera contributes to the source arriving in the next 10 minutes (committed part)
            parts = {}
            W = model.w.reshape(len(model.cams), 3, -1)
            for k in range(1, 11):
                X, _ = model.design(np.array([last]), k, "committed")
                X = np.nan_to_num(X[0].reshape(len(model.cams), 3, -1))
                for i, c in enumerate(model.cams):
                    parts[c] = parts.get(c, 0.0) + float((W[i] * X[i]).sum())
            now["arriving"] = parts
        return {"model": model, "mae": cache["mae"], "mae_persistence": cache["mae_persistence"], "now": now}

    def _publish_transit(self, f, kind, names, t1, value, tr, phys, fan_on_now):
        """Store the transit paths for the dashboard and drive the early warnings."""
        meta = describe(names.get(f, ""))
        now = tr["now"]
        lim = self.limits.get(kind)
        summ = tr["model"].summary(self.volume, self.cam_names) or {}
        out = {"value": value, "unit": meta["unit"], "label": meta["label"], "kind": kind, "lead": self.lead,
               "time": t1.strftime(FMT), "background": _r(phys["cb"]), "removal_per_hour": _r(phys["r"][-1] * 60),
               "removal_fan_per_hour": _r(phys["rates"]["on"] * 60) if phys["rates"].get("on") else None,
               "fan_on": fan_on_now, "paths": {k: [round(v, 3) for v in now[k]] for k in ("committed", "expected", "continue", "fan_on") if k in now},
               "threshold": lim, "model": summ, "mae": _r(tr["mae"], 4), "mae_persistence": _r(tr["mae_persistence"], 4),
               "skill": _r(1 - tr["mae"] / tr["mae_persistence"]) if tr["mae"] and tr["mae_persistence"] else None,
               "arriving": [{"camera": c, "name": self.cam_names.get(c, f"C{c}"), "amount": round(v, 4)}
                            for c, v in sorted(now.get("arriving", {}).items(), key=lambda kv: -kv[1])]}
        peaks = {k: max(v) for k, v in out["paths"].items()}
        out["peak"] = {k: round(v, 3) for k, v in peaks.items()}
        out["committed_rise"] = round(peaks.get("committed", value) - value, 3)
        if lim:
            out["ttt"] = {k: time_to(np.array(v), lim["start"]) for k, v in out["paths"].items()}
        self.db.write_transit_now(f, datetime.now().strftime(FMT), out)
        if lim:
            try:
                self.warnings.observe(f, kind, meta["label"], meta["unit"], t1, value, lim["start"], lim["stop"],
                                      out["ttt"].get("expected"), peaks.get("expected"), peaks.get("committed"),
                                      out["ttt"].get("fan_on"), peaks.get("fan_on"), out["ttt"].get("committed"))
            except Exception as exc:
                print(f"[early warning] {type(exc).__name__}: {exc}")

    def step(self):
        names, sensors, people, issued, context, door_rows, machine_rows = self._load()
        targets = [f for f in FIELDS if canonical(names.get(f, "")) in self.wanted]
        if not targets or len(sensors) < 30:
            self.state.update_forecast(status="Waiting for sensor readings", ready=False)
            return
        t0 = datetime.strptime(sensors[0]["window_start"], FMT)
        t1 = datetime.strptime(sensors[-1]["window_start"], FMT)
        n = int((t1 - t0).total_seconds() // 60) + 1

        def index(ts):
            return int((datetime.strptime(ts, FMT) - t0).total_seconds() // 60)

        series = {f: np.full(n, np.nan) for f in targets}
        for row in sensors:
            i = index(row["window_start"])
            for f in targets:
                if row[f] is not None:
                    series[f][i] = row[f]
        P, M, V, SP = (np.full(n, np.nan) for _ in range(4))
        for row in people:
            i = index(row["window_start"])
            if 0 <= i < n and row["n_person_mean"] is not None:
                nobody = not row["n_person_mean"]
                P[i] = row["n_person_mean"]
                # motion values exist since version 6 (body heights per second); before that
                # they are missing, not 0. Nobody in the area = no motion.
                for arr, name in ((M, "motion_total"), (V, "motion_var"), (SP, "mu_speed")):
                    arr[i] = row[name] if row[name] is not None else (0.0 if nobody else np.nan)
        # each camera input joins the model once there are enough minutes of it
        names_used, series_used = [], []
        for name, arr in zip(DRIVERS, (P, M, V, SP)):
            if name == "persons" or (~np.isnan(arr)).sum() >= self.min_samples:
                names_used.append(name)
                series_used.append(arr)
        # machines / enclosures (E_mach): number running or open per minute. A machine keeps
        # its last reported state (agents may report only changes); joins once there is enough
        machines_on = machine_series(machine_rows, index, n)
        if machines_on is not None and (~np.isnan(machines_on)).sum() >= self.min_samples:
            names_used.append("machines on")
            series_used.append(machines_on)
        drivers = [carry_forward(fill_small_gaps(a, 3), 3) for a in series_used]
        for d in drivers:                              # a few extreme minutes must not steer the fit
            if (~np.isnan(d)).sum() >= 100:
                np.clip(d, None, np.nanpercentile(d, 99), out=d)
        Xd = driver_features(drivers)
        paired = int((~np.isnan(Xd).any(axis=1)).sum())

        # per-camera activity (persons, motion, variance) for the pollution-in-transit model
        zones = {}
        for row in getattr(self, "_cam_rows", []) or []:
            i = index(row["window_start"])
            if not 0 <= i < n:
                continue
            arr = zones.setdefault(row["camera"], np.full((n, 3), np.nan))
            pm = row["n_person_mean"]
            nobody = pm is not None and not pm
            arr[i] = [np.nan if pm is None else pm,
                      row["motion_total"] if row["motion_total"] is not None else (0.0 if nobody else np.nan),
                      row["motion_total_var"] if row["motion_total_var"] is not None else (0.0 if nobody else np.nan)]
        for c in list(zones):
            for j in range(3):
                zones[c][:, j] = carry_forward(fill_small_gaps(zones[c][:, j], 3), 3)
            if np.isfinite(zones[c][:, 0]).sum() < self.min_samples:
                del zones[c]                                  # a camera joins once it has enough minutes

        # ventilation context: the fan only where it was really switched (not dry run);
        # door traffic as people in + out over the last 10 minutes
        fan = np.zeros(n)
        fan_known = False
        for row in context:
            i = index(row["window_start"])
            if 0 <= i < n and row["fan_on"] is not None and not row["dry_run"]:
                fan[i] = row["fan_on"]
                fan_known = True
        door = None
        if door_rows:
            moves = np.zeros(n)
            for row in door_rows:
                i = index(row["window_start"])
                if 0 <= i < n:
                    moves[i] = (row["n_in"] or 0) + (row["n_out"] or 0)
            door = rolling_mean(moves, 10) * 10
            door[np.isnan(door)] = 0.0

        now, now_text = datetime.now(), datetime.now().strftime(FMT)
        fresh = (now - t1).total_seconds() <= 6 * 60
        model_rows, forecast_rows, physics_rows, full_used = [], [], [], 0
        for f in targets:
            y = fill_small_gaps(series[f], 5)
            Xs = sensor_features(y)
            res = evaluate_and_fit(y, Xs, Xd, self.lead, self.min_samples)
            if res["model"] is None:
                continue
            kind = canonical(names.get(f, ""))
            phys = gb = None
            if self.physics_on:
                phys = greybox.physics_inputs(y, kind, P, M, fan if fan_known else None, door,
                                              self.default_rate, self.volume,
                                              extra=[np.nan_to_num(machines_on)] if "machines on" in names_used else None)
                use_d = Xd if paired >= self.min_samples else None
                gb = greybox.compare(y, Xs, use_d, phys, self.lead, self.min_samples, mu=self.mu)
                if not gb.get("models"):
                    gb = None
            if gb is not None:
                # the physics comparison decides: same test minutes for every variant
                m = gb["mae"]
                res["used"] = {"data": "full" if use_d is not None else "sensors"}.get(gb["used"], gb["used"])
                res["mae_persistence"] = m["persistence"]
                res["n"] = gb["n"]
                res["limits"] = gb["limits"]
                if use_d is not None:
                    res["mae_full"] = m["data"]
                else:
                    res["mae_sensors"], res["mae_full"] = m["data"], None

            # pollution in transit: per-camera sources with learned travel times (transit.py)
            tr = None
            if phys is not None and zones:
                try:
                    tr = self._transit(f, y, phys, zones)
                except Exception as exc:                     # never take the forecast down
                    print(f"[transit] {type(exc).__name__}: {exc}")
            if tr is not None and tr["mae"] is not None and res["mae_persistence"]:
                own_skill = 1 - tr["mae"] / tr["mae_persistence"]
                chosen = {"physics": "physics", "greybox": "greybox", "physreg": "physreg", "full": "data",
                          "sensors": "data"}.get(res["used"])
                chosen_skill = (1 - gb["mae"][chosen] / gb["mae"]["persistence"]) if gb is not None and chosen else 0.0
                if own_skill > max(0.0, chosen_skill):
                    res["used"] = "transit"

            # travel time: the driver that is most clearly followed by a rise of this sensor
            best = (None, None, None)
            if paired >= self.min_samples:
                for name, d in zip(names_used, drivers):
                    tau, r = travel_time(d, y)
                    if r is not None and (best[2] is None or r > best[2]):
                        best = (name, tau, r)

            # learning from its own misses: forecasts already issued vs what really happened
            misses, naive = [], []
            for row in issued:
                if row["field"] != f:
                    continue
                i = index(row["predicted_for"])
                if 0 <= i < n and not np.isnan(series[f][i]) and row["model_value"] is not None \
                        and row["base_value"] is not None:
                    misses.append(row["model_value"] - series[f][i])
                    naive.append(row["base_value"] - series[f][i])
            recent_misses = misses[-30:]
            bias = float(np.mean(recent_misses)) if len(recent_misses) >= 5 else 0.0
            live_mae = float(np.mean(np.abs(misses))) if len(misses) >= 5 else None
            live_naive = float(np.mean(np.abs(naive))) if len(naive) >= 5 else None

            # paused while the model's own recent forecasts were worse than "no change"
            paused = (res["used"] != "no change" and len(misses) >= 10 and live_mae > live_naive)
            row = [f, now_text, res["n"], res["mae_persistence"], res["mae_sensors"],
                   res["mae_full"], "paused" if paused else res["used"],
                   best[0], best[1], best[2], live_mae, live_naive, len(misses)]
            if phys is not None:
                rates = phys["rates"]
                lam_now = phys["lambda_online"][np.isfinite(phys["lambda_online"])]
                row += [gb["mae"].get("physics") if gb else None, gb["mae"].get("greybox") if gb else None,
                        gb["mae"].get("physreg") if gb else None, round(phys["cb"], 3),
                        round(rates["off"] * 60, 3) if rates["off"] is not None else None,
                        round(rates["on"] * 60, 3) if rates.get("on") is not None else None,
                        rates["n_off"] + rates["n_on"], rates["source"],
                        round(float(lam_now[-1]) * 60, 3) if len(lam_now) else None,
                        round(phys["co2_gen_lpm"], 3) if phys["co2_gen_lpm"] is not None else None,
                        round(tr["mae"] * res["mae_persistence"] / tr["mae_persistence"], 4)
                        if tr is not None and tr["mae"] is not None and tr["mae_persistence"] else None]
            model_rows.append(tuple(row))

            last = n - 1                                       # newest sensor minute
            if not fresh or np.isnan(Xs[last]).any():
                continue
            if res["used"] == "no change":
                delta = 0.0
            elif res["used"] == "transit" and tr is not None and tr["now"] is not None:
                delta = float(tr["now"]["expected"][self.lead - 1]) - float(y[last])
                full_used += 1
            elif gb is not None and res["used"] in ("physics", "greybox", "physreg", "full", "sensors"):
                name = {"full": "data", "sensors": "data"}.get(res["used"], res["used"])
                if not gb["Z_ok"](last):
                    # a camera input of this minute is missing: the mass balance alone still works
                    if not np.isfinite(gb["dphys"][last]):
                        continue
                    name = "physics"
                delta = float(gb["predict"](name, np.array([last]))[0])
                if name != "data" or use_d is not None:
                    full_used += 1
            elif res["used"] == "full" and not np.isnan(Xd[last]).any():
                delta = res["model_full"].predict(np.column_stack([Xs[last:last + 1], Xd[last:last + 1]]))[0]
                full_used += 1
            else:
                delta = res["model"].predict(Xs[last:last + 1])[0]
            if res["used"] != "no change":
                delta = float(np.clip(delta - bias, *res["limits"]))
            base = float(y[last])
            model_value = max(0.0, base + delta)
            if phys is not None:
                lam_last = phys["lambda_online"][last]
                dp = gb["dphys"][last] if gb is not None else greybox.physics_delta(y, phys, self.lead)[last]
                physics_rows.append((t1.strftime(FMT), f, round(base, 3), round(phys["cb"], 3),
                                     round(float(phys["r"][last]) * 60, 4),
                                     None if not np.isfinite(lam_last) else round(float(lam_last) * 60, 4),
                                     None if not np.isfinite(phys["s_recent"][last]) else round(float(phys["s_recent"][last]), 4),
                                     None if not np.isfinite(dp) else round(max(0.0, base + float(dp)), 3),
                                     round(base if paused else model_value, 3), "paused" if paused else res["used"]))
                # physics and grey-box events (only when something changes)
                occ = (P > 0) & np.isfinite(phys["s_recent"])
                ref = float(np.nanpercentile(phys["s_recent"][occ], 95)) if occ.sum() >= 60 else None
                m_ = gb["mae"] if gb is not None else {}
                skill = {k: round(1 - v / m_["persistence"], 3) for k, v in m_.items()
                         if k != "persistence" and m_.get("persistence")}
                if "data" in skill:
                    skill["full"] = skill["sensors"] = skill["data"]
                meta = describe(names.get(f, ""))
                try:
                    self.events.observe(kind, meta["label"], meta["unit"], {
                        "used": res["used"], "paused": paused, "skill": skill, "value": base,
                        "rates": {**phys["rates"], "now": float(phys["r"][last])}, "background": phys["cb"],
                        "lambda_online": None if not np.isfinite(lam_last) else float(lam_last),
                        "source": None if not np.isfinite(phys["s_recent"][last]) else float(phys["s_recent"][last]),
                        "source_ref": ref if ref and ref > 0 else None,
                        "persons": None if not np.isfinite(P[last]) else float(P[last]),
                        "co2_gen_lpm": phys["co2_gen_lpm"]})
                except Exception as exc:            # events must never stop the forecast
                    print(f"[physics events] {type(exc).__name__}: {exc}")
            if tr is not None and tr["now"] is not None:
                self._publish_transit(f, kind, names, t1, float(y[last]), tr, phys, fan_known and bool(fan[last]))
            forecast_rows.append(((t1 + timedelta(minutes=self.lead)).strftime(FMT), f, now_text,
                                  self.lead, round(base if paused else model_value, 3),
                                  round(base, 3), round(model_value, 3)))
        self.db.write_forecast_models(model_rows)
        self.db.write_forecasts(forecast_rows)
        self.db.write_physics_minute(physics_rows)

        if not model_rows:
            status = (f"Collecting data: the forecast starts after {self.min_samples} usable "
                      "minutes of sensor readings.")
        elif paired < self.min_samples:
            status = (f"Forecasting from the sensors' own history for now. Persons and motion join "
                      f"in after {self.min_samples} minutes recorded together with the sensors "
                      f"({paired} so far).")
        else:
            status = (f"Trained on {paired} minutes of persons, motion and sensor readings; "
                      f"{full_used} of {len(forecast_rows)} forecasts use persons and motion.")
        self.state.update_forecast(status=status, ready=bool(forecast_rows), paired=paired,
                                   needed=self.min_samples, lead=self.lead, updated=now_text)
