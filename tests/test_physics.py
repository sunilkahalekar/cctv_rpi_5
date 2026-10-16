"""Mass balance: removal rate and CO2 generation recovered from simulated lab days; the
grey-box variants beat "no change"; the forecaster stores the physics results."""
from datetime import datetime, timedelta

import numpy as np

from iaqcam import greybox as G
from iaqcam import physics as P
from iaqcam.forecast import driver_features, sensor_features
from sim_room import LAMBDA_OFF, LAMBDA_ON, K_DEP, G_LPM, V, simulate


def test_phi_closed_form_matches_stepping():
    c, cb, r, s = 900.0, 420.0, 0.03, 2.0
    x = c
    for _ in range(10):
        x = x + (s - r * (x - cb))
    exact = P.phi(c, cb, r, s, 10)
    assert abs(exact - x) < 3.0                     # Euler with 1-min steps vs exact solution
    assert abs(P.phi(c, cb, 0.0, s, 10) - (c + 10 * s)) < 1e-9


def test_removal_rates_from_decays():
    d = simulate(days=10, seed=3)
    for kind, true_off in (("co2", LAMBDA_OFF), ("pm2_5", LAMBDA_OFF + K_DEP)):
        y = d[kind]
        cb = P.background(P.smooth(y, 5), kind)
        rates = P.removal_rate(y, d["persons"], cb, d["fan"])
        assert rates["source"] == "decay" and rates["n_off"] >= 3
        assert abs(rates["off"] - true_off) / true_off < 0.25
    rates = P.removal_rate(d["pm2_5"], d["persons"], P.background(P.smooth(d["pm2_5"], 5), "pm2_5"), d["fan"])
    assert rates["on"] is not None and rates["on"] > 2 * rates["off"]       # fan raises removal


def test_co2_generation_per_person_from_camera_count():
    d = simulate(days=4, seed=5)
    cb = P.background(P.smooth(d["co2"], 5), "co2")
    _, a, _ = P.online_lambda(d["co2"], cb, d["persons"], d["motion"])
    gen = P.co2_generation_lpm(float(np.nanmedian(a)), V)
    assert abs(gen - G_LPM) / G_LPM < 0.25


def test_variants_beat_no_change():
    d = simulate(days=10, seed=3)
    Xd = driver_features([d["persons"], d["motion"]])
    for kind in ("co2", "pm2_5"):
        y = d[kind]
        phys = G.physics_inputs(y, kind, d["persons"], d["motion"], d["fan"], None, 1 / 60, V)
        res = G.compare(y, sensor_features(y), Xd, phys, 10, 180)
        m = res["mae"]
        assert m["physics"] < m["persistence"]
        assert min(m["greybox"], m["physreg"]) < m["persistence"]
        assert res["used"] in G.VARIANTS


def test_greybox_survives_unseen_ventilation():
    """Leave-one-condition-out in miniature: the fan never runs while people are present in
    the training days and runs every afternoon in the test days. The physics carries the
    new removal rate (measured from empty-room decays), so the grey-box model degrades less
    than the data-only model."""
    a = simulate(days=4, seed=7, fan_hours={18})                     # fan only while the room empties
    b = simulate(days=4, seed=9, fan_hours=set())                    # fan never
    train_d = {k: np.concatenate([a[k], b[k]]) for k in ("pm2_5", "persons", "motion", "fan")}
    test_d = simulate(days=3, seed=8, fan_hours={13, 14, 15, 16})
    y = np.concatenate([train_d["pm2_5"], test_d["pm2_5"]])
    pers = np.concatenate([train_d["persons"], test_d["persons"]])
    mot = np.concatenate([train_d["motion"], test_d["motion"]])
    fan = np.concatenate([train_d["fan"], test_d["fan"]])
    n_train = len(train_d["pm2_5"])
    Xs, Xd = sensor_features(y), driver_features([pers, mot])
    phys = G.physics_inputs(y[:n_train], "pm2_5", pers[:n_train], mot[:n_train], fan[:n_train], None, 1 / 60, V)
    # rates measured on the training days only, applied to all days
    full = G.physics_inputs(y, "pm2_5", pers, mot, fan, None, 1 / 60, V)
    full["rates"], full["cb"] = phys["rates"], phys["cb"]
    full["r"] = P.rate_series(phys["rates"], fan, len(y))
    full["s_obs"] = P.observed_source(y, full["cb"], full["r"])
    full["ctx"] = np.column_stack([fan, full["r"] * 60])
    target = np.full(len(y), np.nan)
    target[:-10] = y[10:] - y[:-10]
    rows = np.arange(len(y))
    train = rows[(rows < n_train - 40) & ~np.isnan(target) & ~np.isnan(Xd).any(axis=1)]
    test = rows[(rows >= n_train) & ~np.isnan(target) & (fan == 1) & ~np.isnan(Xd).any(axis=1)]
    Zd = np.column_stack([Xs, Xd])
    Zg = np.column_stack([Zd, full["ctx"]])
    em = G.Emission().fit(Xd, full["s_obs"], train)
    dphys = G.physics_delta(y, full, 10, em.source(Xd))
    data = G.Ridge().fit(Zd[train], target[train])
    grey = G.Ridge(alpha=3.0).fit(Zg[train], target[train] - dphys[train])
    mae_data = np.mean(np.abs(data.predict(Zd[test]) - target[test]))
    mae_grey = np.mean(np.abs(dphys[test] + grey.predict(Zg[test]) - target[test]))
    print("unseen fan: data", mae_data, "grey-box", mae_grey, "rates", full["rates"])
    assert full["rates"]["on"] is not None
    assert mae_grey < mae_data


def test_forecaster_stores_physics(tmp_path):
    from iaqcam.db import Database
    from iaqcam.forecast import Forecaster

    class State:
        def update_forecast(self, **kw):
            self.kw = kw

    d = simulate(days=3, seed=2)
    n = len(d["co2"])
    db = Database(tmp_path / "t.db")
    db.write_layout([(1, "C1", "lab", "main")], [{"key": "main", "name": "Main", "inside": True, "sort": 0}])
    db.write_sensor_fields({"field1": "CO2", "field2": "PM2.5"})
    t0 = datetime.now().replace(second=0, microsecond=0) - timedelta(minutes=n - 1)
    sens, mins, ctx = [], [], []
    for i in range(n):
        ts = (t0 + timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
        sens.append({"entry_id": i + 1, "created_at_utc": ts, "ts_local": ts, "window_start": ts,
                     **{f"field{k}": None for k in range(1, 9)}, "field1": float(d["co2"][i]),
                     "field2": float(d["pm2_5"][i])})
        mins.append({"window_start": ts, "camera": 1, "camera_name": "C1", "location": "lab",
                     "n_person": int(d["persons"][i]), "n_person_mean": float(d["persons"][i]),
                     "n_moving_mean": 0.0, "motion_total": float(d["motion"][i]), "motion_total_var": 0.001,
                     "mu_speed": 0.1, "n_samples": 60})
        ctx.append(ts)
    db.write_sensor_entries(sens)
    db.write_minutes(mins)
    for i, ts in enumerate(ctx):
        if d["fan"][i] or i % 30 == 0:
            db.write_context_minute(ts, int(d["fan"][i]), int(d["fan"][i]), 0, "manual")
    cfg = {"forecast": {"lead_minutes": 10, "min_samples": 180, "history_days": 14, "fields": ["co2", "pm2_5"]},
           "sensor_zone": None, "database": {"path": str(tmp_path / "t.db")},
           "physics": {"enabled": True, "volume_m3": V, "default_ach": 1.0, "physics_weight": 1.0}}
    fc = Forecaster(cfg, db, State())
    fc.step()
    con = db._con
    rows = {r[0]: r for r in con.execute("SELECT field, used, mae_physics, mae_greybox, removal_per_hour, "
                                         "co2_gen_lpm FROM forecast_models")}
    assert rows["field1"][2] is not None and rows["field1"][4] is not None
    assert rows["field1"][1] in ("physics", "greybox", "physreg", "full", "sensors", "no change")
    assert rows["field2"][3] is not None
    assert abs(rows["field1"][4] - LAMBDA_OFF * 60) < 0.6


class FakeDb:
    def __init__(self):
        self.state, self.events = {}, []

    def get_state(self, k, default=None):
        return self.state.get(k, default)

    def set_state(self, k, v):
        self.state[k] = str(v)

    def write_event(self, kind, message, level="info", **kw):
        self.events.append((kind, level, message))


def _info(**kw):
    base = {"used": "greybox", "paused": False, "skill": {"greybox": 0.4}, "value": 20.0,
            "rates": {"off": 0.035, "on": None, "n_off": 3, "n_on": 0, "source": "decay", "now": 0.035},
            "background": 25.0, "lambda_online": None, "source": 0.1, "source_ref": 1.0, "persons": 3,
            "co2_gen_lpm": None}
    base.update(kw)
    return base


def test_physics_events_log_changes_only():
    from iaqcam.physics_events import PhysicsEvents
    db = FakeDb()
    ev = PhysicsEvents(db, {"pm2_5": {"start": 35, "stop": 25}})
    ev.observe("pm2_5", "PM2.5", "µg/m³", _info())
    kinds = [e[0] for e in db.events]
    assert kinds.count("model") == 1 and kinds.count("physics") == 1          # variant + removal measured
    n = len(db.events)
    ev.observe("pm2_5", "PM2.5", "µg/m³", _info())                               # nothing changed
    assert len(db.events) == n
    ev.observe("pm2_5", "PM2.5", "µg/m³", _info(paused=True))
    assert db.events[-1][1] == "warning" and "paused" in db.events[-1][2]
    # steady level above the start threshold for 3 minutes: one warning
    for _ in range(4):
        ev.observe("pm2_5", "PM2.5", "µg/m³", _info(source=1.5))                # 25 + 1.5/0.035 = 68
    steady = [e for e in db.events if "heads to" in e[2]]
    assert len(steady) == 1 and steady[0][1] == "warning"
    # fan measured: weak fan warns
    ev.observe("pm2_5", "PM2.5", "µg/m³", _info(rates={"off": 0.035, "on": 0.04, "n_off": 3, "n_on": 2,
                                                        "source": "decay", "now": 0.035}))
    assert "barely helps" in db.events[-1][2]
    # rising with nobody present
    for _ in range(3):
        ev.observe("pm2_5", "PM2.5", "µg/m³", _info(persons=0, source=2.0))
    assert any("nobody" in e[2] for e in db.events)
    # restart: state persists, nothing repeated
    n = len(db.events)
    PhysicsEvents(db, {"pm2_5": {"start": 35, "stop": 25}}).observe("pm2_5", "PM2.5", "µg/m³", _info(persons=0, source=2.0, rates={"off": 0.035, "on": 0.04, "n_off": 3, "n_on": 2, "source": "decay", "now": 0.035}))
    assert len(db.events) == n


def test_sealed_room_event_for_co2():
    from iaqcam.physics_events import PhysicsEvents
    db = FakeDb()
    ev = PhysicsEvents(db)
    info = _info(rates={"off": 0.025, "on": None, "n_off": 4, "n_on": 0, "source": "decay", "now": 0.025},
                 lambda_online=0.008, persons=8, co2_gen_lpm=0.3, background=420.0, value=900.0)
    for _ in range(10):
        ev.observe("co2", "CO₂", "ppm", info)
    assert any("Air change low" in e[2] for e in db.events)
    ev.observe("co2", "CO₂", "ppm", dict(info, lambda_online=0.02))
    assert "back to normal" in db.events[-1][2]
    ev.observe("co2", "CO₂", "ppm", dict(info, co2_gen_lpm=0.9))
    assert "CO2 per person" in db.events[-1][2]
