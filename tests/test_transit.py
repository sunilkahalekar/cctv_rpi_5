"""Pollution in transit: travel times and emission factors recovered from a simulated lab
with a near and a far work zone; forecasts beat no-change and the well-mixed physics;
early warnings come before the sensor crosses the threshold."""
import numpy as np

from iaqcam import physics as P
from iaqcam.transit import TransitModel, lead_time_eval, ttt_from_paths
from sim_zones import DELAYS, simulate


def _setup():
    d = simulate(days=8, seed=4)
    y = d["pm2_5"]
    n = len(y)
    cb = P.background(P.smooth(y, 5), "pm2_5")
    rates = P.removal_rate(y, d["persons"], cb, d["fan"])
    r = P.rate_series(rates, d["fan"], n)
    s_obs = P.observed_source(y, cb, r)
    cut = int(n * 0.75)
    model = TransitModel().fit(d["zones"], s_obs, np.arange(40, cut - 40))
    return d, y, cb, r, model, cut


def test_travel_times_and_emission_factors():
    d, y, cb, r, model, cut = _setup()
    s = model.summary(300.0)
    travel = {c["camera"]: c["travel_min"] for c in s["cameras"]}
    assert abs(travel[1] - DELAYS[1]) < 1.5 and abs(travel[2] - DELAYS[2]) < 2.0
    assert travel[2] > travel[1] + 4                       # the far zone is recognised as far
    ef = s["emission_per_min"]
    assert abs(ef["motion"] - 0.9) < 0.2 and s["r2"] > 0.8


def test_forecast_beats_persistence_and_well_mixed():
    d, y, cb, r, model, cut = _setup()
    n = len(y)
    test = np.arange(cut, n - 30)
    p = model.paths(test, y[test], cb, r[test], horizon=10)
    ok = p["continue_ok"]
    tgt = y[test + 10]
    mae_t = np.mean(np.abs(p["continue"][ok, 9] - tgt[ok]))
    mae_p = np.mean(np.abs(y[test][ok] - tgt[ok]))
    k = 10
    s_rec = (y[test] - y[test - k]) / k + r[test] * (np.convolve(y, np.ones(k) / k, "full")[:n][test] - cb)
    mae_w = np.mean(np.abs(P.phi(y[test], cb, r[test], s_rec, 10)[ok] - tgt[ok]))
    assert mae_t < 0.6 * mae_p and mae_t < 0.6 * mae_w
    # committed never above continue (stopping work can only lower the path)
    assert np.all(p["committed"][ok] <= p["continue"][ok] + 1e-9)


def test_early_warning_beats_sensor_trend_and_well_mixed():
    """30 simulated days: train on 10, test on 20. The 'expected' path warns earlier than the
    well-mixed physics with fewer false alarms than extrapolating the sensor's trend; the
    'committed' path never raises a false alarm."""
    d = simulate(days=30, seed=9)
    y = d["pm2_5"]
    n = len(y)
    cb = P.background(P.smooth(y, 5), "pm2_5")
    rates = P.removal_rate(y, d["persons"], cb, d["fan"])
    r = P.rate_series(rates, d["fan"], n)
    model = TransitModel().fit(d["zones"], P.observed_source(y, cb, r), np.arange(40, 10 * 1440))
    te = np.arange(10 * 1440 + 40, n - 31)
    p = model.paths(te, y[te], cb, r[te], horizon=30)
    ev = lambda path: lead_time_eval(y, te, ttt_from_paths(path, 50.0), 50.0, 40.0)   # noqa: E731
    exp = ev(np.where(p["expected_ok"][:, None], p["expected"], np.nan))
    com = ev(np.where(p["committed_ok"][:, None], p["committed"], np.nan))
    k = 10
    s_rec = (y[te] - y[te - k]) / k + r[te] * (np.convolve(y, np.ones(k) / k, "full")[:n][te] - cb)
    wm = ev(np.column_stack([P.phi(y[te], cb, r[te], s_rec, h) for h in range(1, 31)]))
    slope = (y[te] - y[te - 5]) / 5
    trend = ev(np.column_stack([y[te] + slope * h for h in range(1, 31)]))
    assert exp["crossings"] >= 20 and exp["missed"] == 0
    assert np.median(exp["confirmed"]) >= np.median(wm["confirmed"]) + 4
    assert exp["false_alarms"] < trend["false_alarms"] and exp["false_alarms"] < wm["false_alarms"]
    assert com["false_alarms"] == 0


class WarnDb:
    def __init__(self):
        self.state, self.events, self.rows = {}, [], {}

    def get_state(self, k, default=None):
        return self.state.get(k, default)

    def set_state(self, k, v):
        self.state[k] = str(v)

    def write_event(self, kind, message, level="info", **kw):
        self.events.append((kind, level, message))

    def open_warning(self, *a):
        wid = len(self.rows) + 1
        self.rows[wid] = {"outcome": "open"}
        return wid

    def close_warning(self, wid, outcome, crossed_at=None, lead_min=None):
        self.rows[wid].update(outcome=outcome, lead=lead_min)

    def write_missed_crossing(self, *a):
        self.rows[len(self.rows) + 1] = {"outcome": "missed"}


def test_early_warning_lifecycle():
    from datetime import datetime, timedelta
    from iaqcam.physics_events import EarlyWarnings
    db, t0 = WarnDb(), datetime(2026, 10, 13, 10, 0)
    ew = EarlyWarnings(db)
    for k in range(2):                                     # 2 minutes of 'continue' reaching 50
        ew.observe("field1", "pm2_5", "PM2.5", "µg/m³", t0 + timedelta(minutes=k), 36, 50, 40, 12, 58, 41, None, 37)
    assert db.rows[1]["outcome"] == "open" and "Early warning" in db.events[-1][2] and "stays below" in db.events[-1][2]
    assert ew.observe("field1", "pm2_5", "PM2.5", "µg/m³", t0 + timedelta(minutes=12), 51, 50, 40, None, 55, 52) == "confirmed"
    assert db.rows[1]["lead"] == 11.0
    # stays above: nothing new; drops below stop and rises again without warning -> missed
    ew.observe("field1", "pm2_5", "PM2.5", "µg/m³", t0 + timedelta(minutes=13), 53, 50, 40, None, 55, 52)
    ew.observe("field1", "pm2_5", "PM2.5", "µg/m³", t0 + timedelta(minutes=40), 35, 50, 40, None, 36, 35)
    assert ew.observe("field1", "pm2_5", "PM2.5", "µg/m³", t0 + timedelta(minutes=41), 50.5, 50, 40, None, 52, 51) == "missed"
    # a warning that never comes true becomes a false alarm after 40 min
    ew.observe("field1", "pm2_5", "PM2.5", "µg/m³", t0 + timedelta(minutes=60), 30, 50, 40, None, 31, 30)
    for k in range(2):
        ew.observe("field1", "pm2_5", "PM2.5", "µg/m³", t0 + timedelta(minutes=61 + k), 30, 50, 40, 20, 51, 31)
    assert ew.observe("field1", "pm2_5", "PM2.5", "µg/m³", t0 + timedelta(minutes=110), 30, 50, 40, None, 31, 30) == "false alarm"
