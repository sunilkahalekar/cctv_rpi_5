"""
Offline evaluation for the paper: physics-informed and grey-box forecasting
(EAMTP research plan, sections 5.2 and 6).

    python research/greybox_eval.py                       # uses config.yaml and its database
    python research/greybox_eval.py --demo                # simulated lab days (checks the pipeline)
    python research/greybox_eval.py --fields co2 pm2_5 --epochs 40 --out research_out

What it does
------------
1. Builds one row per minute from the app's database: every sensor, persons / total motion /
   motion variance / speed seen by the cameras inside the areas (inside_minute, or the
   sensor's zone), the exhaust-fan state when really switched (context_minute, not dry
   run) and door traffic (door_minute). Saved as dataset.csv.
2. Splits (plan section 6), each with an embargo gap of window + lead minutes:
     rung0   chronological: first 75 % train, last 25 % test
     rung1   leave-one-day-out (every day with enough minutes is the test set once)
     rung2   leave-one-condition-out: test = minutes with the fan really on, train = fan off
             (only when such minutes exist)
     rung4   forward chaining: train on days 1..k, test on day k+1
   Physics constants (background, removal rates, emission model) are estimated on the
   training rows of each fold only.
3. Models compared on every fold and pollutant:
     persistence, physics (mass balance only), ridge (data), ridge grey-box residual,
     ridge with physics-informed loss, and when PyTorch is installed:
     bilstm (data), bilstm_greybox (physics + BiLSTM residual), bilstm_pinn (BiLSTM with
     the mass-balance penalty in the loss), film_greybox (grey-box whose BiLSTM is
     modulated by the context: fan, removal rate, door traffic - FiLM).
4. Writes metrics.csv (every fold), summary.md (mean per rung and model: MAE, RMSE,
   skill = 1 - MAE / MAE persistence) and physics.json (removal rates, background,
   CO2 generation per person).
"""
import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from iaqcam import greybox as G                                    # noqa: E402
from iaqcam import physics as P                                    # noqa: E402
from iaqcam.forecast import (driver_features, fill_small_gaps, rolling_mean,  # noqa: E402
                             sensor_features)
from iaqcam.sensor_meta import canonical                           # noqa: E402
from iaqcam.transit import TransitModel, lead_time_eval, ttt_from_paths   # noqa: E402

FMT = "%Y-%m-%d %H:%M:%S"
DRIVERS = ("persons", "motion", "motion_var", "speed")


# ------------------------------------------------------------------ data
def load_db(db_path, zone=None, inside_cams=()):
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    names = dict(con.execute("SELECT field, name FROM sensor_fields").fetchall())
    sens = con.execute("SELECT * FROM sensor_minute ORDER BY window_start").fetchall()
    src = ("zone_minute WHERE zone = ?", (zone,)) if zone else ("inside_minute WHERE 1 = 1", ())
    ppl = con.execute("SELECT window_start, n_person_mean, motion_total, motion_var, mu_speed FROM "
                      + src[0], src[1]).fetchall()
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    ctx = con.execute("SELECT window_start, fan_on, dry_run FROM context_minute").fetchall() \
        if "context_minute" in tables else []
    door = con.execute("SELECT window_start, n_in, n_out FROM door_minute").fetchall() if "door_minute" in tables else []
    machines = con.execute("SELECT window_start, machine, on_state FROM machine_minute ORDER BY window_start").fetchall() \
        if "machine_minute" in tables else []
    cams = con.execute("SELECT window_start, camera, n_person_mean, motion_total, motion_total_var FROM camera_minute "
                       f"WHERE camera IN ({', '.join('?' * len(inside_cams))})", tuple(inside_cams)).fetchall() \
        if inside_cams else []
    con.close()
    if not sens:
        sys.exit("No sensor minutes in the database.")
    t0 = datetime.strptime(sens[0]["window_start"], FMT)
    t1 = datetime.strptime(sens[-1]["window_start"], FMT)
    n = int((t1 - t0).total_seconds() // 60) + 1
    idx = lambda ts: int((datetime.strptime(ts, FMT) - t0).total_seconds() // 60)   # noqa: E731
    data = {"time": [(t0.timestamp() + 60 * i) for i in range(n)]}
    for f, name in names.items():
        key = canonical(name)
        arr = np.full(n, np.nan)
        for r in sens:
            if r[f] is not None:
                arr[idx(r["window_start"])] = r[f]
        data[key] = arr
    for k in DRIVERS + ("fan", "door"):
        data[k] = np.full(n, np.nan)
    for r in ppl:
        i = idx(r["window_start"])
        if 0 <= i < n and r["n_person_mean"] is not None:
            nobody = not r["n_person_mean"]
            data["persons"][i] = r["n_person_mean"]
            for k, col in (("motion", "motion_total"), ("motion_var", "motion_var"), ("speed", "mu_speed")):
                data[k][i] = r[col] if r[col] is not None else (0.0 if nobody else np.nan)
    data["fan"][:] = 0.0
    for r in ctx:
        i = idx(r["window_start"])
        if 0 <= i < n and r["fan_on"] is not None and not r["dry_run"]:
            data["fan"][i] = r["fan_on"]
    moves = np.zeros(n)
    for r in door:
        i = idx(r["window_start"])
        if 0 <= i < n:
            moves[i] = (r["n_in"] or 0) + (r["n_out"] or 0)
    data["door"] = np.nan_to_num(rolling_mean(moves, 10) * 10)
    from iaqcam.forecast import machine_series
    ms_ = machine_series(machines, lambda ts: idx(ts), n)
    if ms_ is not None and np.isfinite(ms_).sum() >= 180:       # E_mach joins once recorded
        data["machines"] = ms_
    # per-camera activity for the pollution-in-transit model
    zones = {}
    for r in cams:
        i = idx(r["window_start"])
        if not 0 <= i < n:
            continue
        a = zones.setdefault(r["camera"], np.full((n, 3), np.nan))
        nobody = r["n_person_mean"] is not None and not r["n_person_mean"]
        a[i] = [np.nan if r["n_person_mean"] is None else r["n_person_mean"],
                r["motion_total"] if r["motion_total"] is not None else (0.0 if nobody else np.nan),
                r["motion_total_var"] if r["motion_total_var"] is not None else (0.0 if nobody else np.nan)]
    for c in list(zones):
        for j in range(3):
            zones[c][:, j] = fill_small_gaps(zones[c][:, j], 3)
        if np.isfinite(zones[c][:, 0]).sum() < 180:
            del zones[c]
    if zones:
        data["zones"] = zones
    if "events" in tables:                                   # experiment log (Events page)
        exps, cur = [], {}
        for r in con.execute("SELECT ts, user, details FROM events WHERE kind = 'experiment' ORDER BY id"):
            try:
                d = json.loads(r["details"] or "{}")
            except ValueError:
                continue
            if d.get("action") == "stop":
                exps.append([d.get("start"), d.get("end") or r["ts"], d.get("type"), d.get("note") or "",
                             d.get("end_note") or "", r["user"] or ""])
            elif d.get("action") == "note":
                exps.append([r["ts"], r["ts"], "note", d.get("note") or "", "", r["user"] or ""])
        data["experiments"] = exps
    return data


def demo_data(days=12):
    sys.path.insert(0, str(ROOT / "tests"))
    from sim_room import simulate
    parts = [simulate(days=days // 2, seed=11, fan_hours=set()),
             simulate(days=days - days // 2, seed=12, fan_hours={13, 14, 18})]
    data = {k: np.concatenate([p[k] for p in parts]) for k in ("co2", "pm2_5", "persons", "motion", "fan")}
    n = len(data["co2"])
    data["time"] = [datetime(2026, 1, 1).timestamp() + 60 * i for i in range(n)]
    data["motion_var"] = data["motion"] * 0.1
    data["speed"] = np.minimum(0.4, data["motion"] / np.maximum(1, data["persons"]))
    data["door"] = np.zeros(n)
    return data


def demo_zones_data(days=12):
    """Simulated lab with a near and a far work zone (tests/sim_zones.py), fan in the second half."""
    sys.path.insert(0, str(ROOT / "tests"))
    from sim_zones import simulate
    parts = [simulate(days=days // 2, seed=21), simulate(days=days - days // 2, seed=22, fan_hours=(13, 14, 18))]
    data = {"pm2_5": np.concatenate([p["pm2_5"] for p in parts]),
            "fan": np.concatenate([p["fan"] for p in parts])}
    data["zones"] = {z: np.concatenate([p["zones"][z] for p in parts]) for z in (1, 2)}
    n = len(data["pm2_5"])
    Z = data["zones"]
    data["persons"] = Z[1][:, 0] + Z[2][:, 0]
    data["motion"] = Z[1][:, 1] + Z[2][:, 1]
    data["motion_var"] = Z[1][:, 2] + Z[2][:, 2]
    data["speed"] = np.full(n, 0.1)
    data["door"] = np.zeros(n)
    data["time"] = [datetime(2026, 1, 1).timestamp() + 60 * i for i in range(n)]
    return data


# ------------------------------------------------------------- features
def prepare(data, field, lead, window):
    y = fill_small_gaps(data[field], 5)
    keys = list(DRIVERS) + (["machines"] if "machines" in data else [])
    drivers = [fill_small_gaps(data[k], 3) for k in keys if np.isfinite(data[k]).sum() > 100]
    Xs = sensor_features(y)
    Xd = driver_features(drivers) if drivers else None
    target = np.full(len(y), np.nan)
    target[:-lead] = y[lead:] - y[:-lead]
    seq = np.column_stack([y] + drivers + [data["fan"], data["door"]])
    return y, Xs, Xd, target, seq


def physics_for_fold(y, field, data, train, volume, default_rate, Xd, lead, calibration=False):
    """Physics with constants from the training rows only. calibration=True (leave-condition-
    out): the removal rates may also use empty-room decays of the held-out condition - the
    decay test of plan 4.1 measures lambda of a new ventilation state without any forecast
    target of it being seen (test minutes are occupied minutes only)."""
    pers_train = np.full(len(y), np.nan)
    pers_train[train] = data["persons"][train]
    if calibration:
        empty = data["persons"] == 0
        pers_train[empty] = 0.0
    y_train = np.full(len(y), np.nan)
    y_train[train] = y[train]
    cb = P.background(P.smooth(y_train, 5), field)
    if cb is None:
        return None
    fan = data["fan"] if np.nansum(data["fan"]) > 0 else None
    rates = P.removal_rate(y, pers_train, cb, fan, default=default_rate)
    r = P.rate_series(rates, fan, len(y))
    gen = None
    if field == "co2":
        lam, a, _ = P.online_lambda(y, cb, data["persons"], data["motion"])
        if np.isfinite(a[train]).sum() >= 30:
            gen = P.co2_generation_lpm(float(np.nanmedian(a[train])), volume)
    k = 10
    s_recent = np.full(len(y), np.nan)
    mean_k = np.convolve(np.nan_to_num(y), np.ones(k) / k, mode="full")[:len(y)]
    s_recent[k:] = (y[k:] - y[:-k]) / k + r[k:] * (mean_k[k:] - cb)
    phys = {"cb": cb, "rates": rates, "r": r, "s_recent": s_recent, "co2_gen_lpm": gen,
            "s_obs": P.observed_source(y, cb, r),
            "ctx": np.column_stack([fan if fan is not None else np.zeros(len(y)), r * 60, data["door"]])}
    src = None
    if Xd is not None:
        em = G.Emission().fit(Xd, phys["s_obs"], train)
        src = em.source(Xd)
        if src is not None:
            src = np.where(np.isnan(src), s_recent, src)
    phys["dphys"] = G.physics_delta(y, phys, lead, src)
    return phys


# ----------------------------------------------------------------- splits
def splits(n, times, usable, fan, lead, window, min_rows=120, max_folds=None):
    gap = window + lead
    days = np.array([datetime.fromtimestamp(t).strftime("%Y-%m-%d") for t in times])
    idx = np.flatnonzero(usable)
    out = []
    if len(idx) >= 4 * min_rows:
        cut = int(len(idx) * 0.75)
        out.append(("rung0", "chronological", idx[:cut][idx[:cut] < idx[cut] - gap], idx[cut:]))
    day_list = [d for d in dict.fromkeys(days[idx]) if (days[idx] == d).sum() >= min_rows]
    if len(day_list) >= 3:
        for d in day_list:
            test = idx[days[idx] == d]
            train = idx[(days[idx] != d) & ((idx < test.min() - gap) | (idx > test.max() + gap))]
            out.append(("rung1", "leave-day-out " + d, train, test))
    if fan is not None and np.nansum(fan[idx]) >= min_rows:
        test = idx[fan[idx] == 1]
        far = np.ones(n, bool)
        for t in test:
            far[max(0, t - gap):t + gap + 1] = False
        train = idx[(fan[idx] == 0) & far[idx]]
        out.append(("rung2", "leave-condition-out: fan on", train, test))
    for k in range(2, len(day_list)):
        train_days, test_day = day_list[:k], day_list[k]
        test = idx[days[idx] == test_day]
        train = idx[np.isin(days[idx], train_days) & (idx < test.min() - gap)]
        out.append(("rung4", "forward " + test_day, train, test))
    out = [s for s in out if len(s[2]) >= min_rows and len(s[3]) >= 30]
    if max_folds:                                   # keep the last folds of each rung
        keep = []
        for rung in dict.fromkeys(s[0] for s in out):
            keep += [s for s in out if s[0] == rung][-max_folds:]
        out = keep
    return out


# ------------------------------------------------------------ neural models
def torch_models(seq, target, dphys, ctx, train, test, window, epochs, mu, seed=0):
    """seq: per-minute inputs, first column the sensor level. The physics-coupled networks get
    the minute-to-minute change instead of the level (plan 5.4), the data-only BiLSTM the level."""
    if epochs <= 0:
        return {}
    seq_change = seq.copy()
    seq_change[1:, 0] = seq[1:, 0] - seq[:-1, 0]
    seq_change[0, 0] = np.nan
    out = {}
    for name, cfg in (("bilstm", (seq, False, False, False)), ("bilstm_greybox", (seq_change, True, False, False)),
                      ("bilstm_pinn", (seq_change, False, True, False)), ("film_greybox", (seq_change, True, False, True))):
        res = _train_one(cfg[0], target, dphys, ctx, train, test, window, epochs, mu, seed, *cfg[1:])
        if res is not None:
            out[name] = res
    return out


def _train_one(seq, target, dphys, ctx, train, test, window, epochs, mu, seed, residual, pinn, film):
    try:
        import torch
        from torch import nn
    except ImportError:
        return None
    torch.manual_seed(seed)
    mean = np.nanmean(seq[train], axis=0)
    std = np.maximum(np.nanstd(seq[train], axis=0), 1e-3 * np.maximum(1.0, np.abs(mean)))
    S = np.clip((seq - mean) / std, -G.LIMIT, G.LIMIT)
    cm = np.nanmean(ctx[train], axis=0)
    cs = np.maximum(np.nanstd(ctx[train], axis=0), 1e-3 * np.maximum(1.0, np.abs(cm)))
    C = np.clip((ctx - cm) / cs, -G.LIMIT, G.LIMIT)
    ts = float(np.nanstd(target[train])) or 1.0

    finite = np.isfinite(S).all(axis=1).astype(float)
    full = np.convolve(finite, np.ones(window), mode="full")[:len(S)] >= window   # whole window known

    def windows(rows):
        rows = rows[(rows >= window - 1)]
        rows = rows[full[rows] & np.isfinite(C[rows]).all(axis=1)]
        X = np.lib.stride_tricks.sliding_window_view(S, window, axis=0)[rows - window + 1].transpose(0, 2, 1)
        return rows, torch.tensor(np.ascontiguousarray(X), dtype=torch.float32), torch.tensor(C[rows], dtype=torch.float32)

    class Net(nn.Module):
        def __init__(self, n_in, n_ctx, film):
            super().__init__()
            self.lstm = nn.LSTM(n_in, 32, batch_first=True, bidirectional=True)
            self.film = nn.Linear(n_ctx, 128) if film else None
            self.head = nn.Sequential(nn.Linear(64, 32), nn.ReLU(), nn.Linear(32, 1))

        def forward(self, x, c):
            h = self.lstm(x)[0][:, -1, :]
            if self.film is not None:
                gamma, beta = self.film(c).chunk(2, dim=1)          # FiLM: context scales and shifts features
                h = h * (1 + gamma) + beta
            return self.head(h).squeeze(1)

    tr_rows, Xtr, Ctr = windows(train)
    te_rows, Xte, Cte = windows(test)
    if len(tr_rows) < 200 or len(te_rows) < 30:
        return None
    y_tr = torch.tensor(target[tr_rows] / ts, dtype=torch.float32)
    p_tr = torch.tensor(np.nan_to_num(dphys[tr_rows]) / ts, dtype=torch.float32)
    if True:
        net = Net(S.shape[1], C.shape[1], film)
        opt = torch.optim.Adam(net.parameters(), lr=1e-3, weight_decay=1e-4)
        n_val = max(50, len(tr_rows) // 7)                          # last part of training: early stopping
        best, best_state, bad = np.inf, None, 0
        for _ in range(epochs):
            net.train()
            perm = torch.randperm(len(tr_rows) - n_val)
            for b in range(0, len(perm), 128):
                i = perm[b:b + 128]
                pred = net(Xtr[i], Ctr[i])
                goal = y_tr[i] - p_tr[i] if residual else y_tr[i]
                loss = ((pred - goal) ** 2).mean()
                if pinn:
                    loss = loss + mu * ((pred - p_tr[i]) ** 2).mean()   # mass-balance penalty
                opt.zero_grad()
                loss.backward()
                opt.step()
            net.eval()
            with torch.no_grad():
                v = slice(len(tr_rows) - n_val, len(tr_rows))
                pv = net(Xtr[v], Ctr[v]) + (p_tr[v] if residual else 0)
                val = float(((pv - y_tr[v]) ** 2).mean())
            if val < best - 1e-5:
                best, best_state, bad = val, {k: t.clone() for k, t in net.state_dict().items()}, 0
            else:
                bad += 1
                if bad >= 6:
                    break
        if best_state is not None:
            net.load_state_dict(best_state)
        net.eval()
        with torch.no_grad():
            pred = net(Xte, Cte).numpy() * ts
        if residual:
            pred = pred + dphys[te_rows]
        return te_rows, pred


# -------------------------------------------------------------------- main
def metrics(pred, true):
    err = pred - true
    return float(np.mean(np.abs(err))), float(np.sqrt(np.mean(err ** 2)))


def lead_times(field, y, te, tm, phys, thr, examples=None):
    """Lead time of the early warning: transit vs well-mixed physics vs the sensor's own trend."""
    start, stop = thr
    te = te[te + 31 < len(y)]
    out = []
    p = tm.paths(te, y[te], phys["cb"], phys["r"][te], horizon=30)
    paths = {"in transit: expected": np.where(p["expected_ok"][:, None], p["expected"], np.nan),
             "in transit: committed (certain)": np.where(p["committed_ok"][:, None], p["committed"], np.nan),
             "in transit: activity held": np.where(p["continue_ok"][:, None], p["continue"], np.nan)}
    k = 10
    n = len(y)
    s_rec = (y[te] - y[te - k]) / k + phys["r"][te] * (np.convolve(np.nan_to_num(y), np.ones(k) / k, "full")[:n][te] - phys["cb"])
    paths["well-mixed physics"] = np.column_stack([P.phi(y[te], phys["cb"], phys["r"][te], s_rec, h) for h in range(1, 31)])
    slope = (y[te] - y[te - 5]) / 5
    paths["sensor trend"] = np.column_stack([y[te] + slope * h for h in range(1, 31)])
    days = round(len(te) / 1440, 1)
    if examples is not None:                 # one worked case for the paper figure: a crossing the
        cand = []                            # expected path saw coming 10 min ahead, else the first one
        for i in range(1, len(te)):
            t = te[i]
            if y[t] >= start and y[t - 1] < start and t - 40 >= te[0] and t + 20 < len(y):
                o = t - 10
                j = int(np.searchsorted(te, o))
                if j >= len(te) or te[j] != o:
                    continue
                hit = bool(np.nanmax(paths["in transit: expected"][j]) >= start)
                cand.append((not hit, len(cand), o, j))
        if cand:
            _, _, o, j = min(cand)
            examples.append({"field": field, "threshold": start, "origin_index": int(o),
                             "actual": [None if not np.isfinite(v) else round(float(v), 3) for v in y[o - 30:o + 31]],
                             "paths": {k: [None if not np.isfinite(v) else round(float(v), 3) for v in paths[k][j]]
                                       for k in paths}})
    for name, path in paths.items():
        res = lead_time_eval(y, te, ttt_from_paths(path, start), start, stop)
        med = float(np.median(res["confirmed"])) if res["confirmed"] else None
        out.append([field, name, res["crossings"], len(res["confirmed"]), res["missed"], res["false_alarms"],
                    None if med is None else round(med, 1), days])
    return out


def run(data, fields, lead, window, epochs, volume, default_ach, mu, outdir, max_folds=None, thresholds=None):
    thresholds = thresholds or {}
    outdir.mkdir(parents=True, exist_ok=True)
    with open(outdir / "dataset.csv", "w", newline="") as fh:
        keys = [k for k in data if k not in ("time", "zones", "experiments")]
        w = csv.writer(fh)
        w.writerow(["time"] + keys)
        for i, t in enumerate(data["time"]):
            w.writerow([datetime.fromtimestamp(t).strftime(FMT)] + [None if not np.isfinite(data[k][i]) else
                                                                     round(float(data[k][i]), 5) for k in keys])
    if data.get("experiments"):
        with open(outdir / "experiments.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["start", "end", "type", "note", "end_note", "user"])
            w.writerows(data["experiments"])
    rows, physics_out, lead_rows, examples = [], {}, [], []
    zones = {k: v for k, v in (data.get("zones") or {}).items()}
    fan = data["fan"] if np.nansum(data["fan"]) > 0 else None
    for field in fields:
        if field not in data:
            print(f"[skip] {field}: not in the database")
            continue
        y, Xs, Xd, target, seq = prepare(data, field, lead, window)
        Zd = Xs if Xd is None else np.column_stack([Xs, Xd])
        usable = np.isfinite(target) & np.isfinite(Zd).all(axis=1)
        folds = splits(len(y), data["time"], usable, fan, lead, window, max_folds=max_folds)
        print(f"{field}: {usable.sum()} usable minutes, {len(folds)} folds")
        for rung, label, train, test in folds:
            phys = physics_for_fold(y, field, data, train, volume, default_ach / 60.0, Xd, lead,
                                    calibration=rung == "rung2")
            if rung == "rung2":
                test = test[data["persons"][test] > 0]          # forecasts only for occupied minutes
            if phys is None:
                continue
            dphys = phys["dphys"]
            Zg = np.column_stack([Xs[:, 1:]] + ([Xd] if Xd is not None else []) + [phys["ctx"]])   # no level (plan 5.4)
            good = lambda r: r[np.isfinite(dphys[r]) & np.isfinite(Zg[r]).all(axis=1)]   # noqa: E731
            tr, te = good(train), good(test)
            if len(tr) < 100 or len(te) < 30:
                continue
            preds = {"persistence": np.zeros(len(te)), "physics": dphys[te],
                     "ridge": G.Ridge().fit(Zd[tr], target[tr]).predict(Zd[te]),
                     "ridge_greybox": dphys[te] + G.Ridge(alpha=3.0).fit(Zg[tr], target[tr] - dphys[tr]).predict(Zg[te]),
                     "ridge_pinn": G.Ridge(mu=mu).fit(Zg[tr], target[tr], prior=dphys[tr]).predict(Zg[te])}
            if zones:                                            # pollution in transit (transit.py)
                tm = TransitModel().fit(zones, phys["s_obs"], tr)
                if tm.ready:
                    p = tm.paths(te, y[te], phys["cb"], phys["r"][te], horizon=lead)
                    ok = p["expected_ok"]
                    pred = p["expected"][:, lead - 1] - y[te]
                    preds["transit"] = np.where(ok, pred, 0.0)
                    if rung == "rung0":
                        physics_out.setdefault(field, {})["transit"] = tm.summary(volume)
                        if thresholds.get(field):
                            lead_rows += lead_times(field, y, te, tm, phys, thresholds[field], examples)
            base_mae = metrics(preds["persistence"], target[te])[0]
            for name, pred in preds.items():
                mae, rmse = metrics(pred, target[te])
                rows.append([field, rung, label, name, len(tr), len(te), mae, rmse, 1 - mae / base_mae if base_mae else None])
            for name, (rws, pred) in torch_models(seq, target, dphys, phys["ctx"], tr, te, window, epochs, mu).items():
                mae, rmse = metrics(pred, target[rws])
                bm = metrics(np.zeros(len(rws)), target[rws])[0]
                rows.append([field, rung, label, name, len(tr), len(rws), mae, rmse, 1 - mae / bm if bm else None])
            if rung == "rung0":
                physics_out.setdefault(field, {}).update({"background": phys["cb"],
                                      "removal_per_hour_fan_off": phys["rates"]["off"] * 60,
                                      "removal_per_hour_fan_on": None if phys["rates"]["on"] is None else phys["rates"]["on"] * 60,
                                      "decay_periods": phys["rates"]["n_off"] + phys["rates"]["n_on"],
                                      "removal_source": phys["rates"]["source"],
                                      "co2_generation_l_per_min_per_person": phys["co2_gen_lpm"],
                                      "volume_m3": volume})
            print(f"  {rung} {label}: " + ", ".join(f"{r[3]} {r[6]:.3f}" for r in rows if r[1] == rung and r[2] == label and r[0] == field))

    with open(outdir / "metrics.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["field", "rung", "fold", "model", "n_train", "n_test", "mae", "rmse", "skill_vs_persistence"])
        w.writerows(rows)
    (outdir / "physics.json").write_text(json.dumps(physics_out, indent=1))
    if examples:
        (outdir / "transit_example.json").write_text(json.dumps(examples, indent=1))
    lines = ["# Grey-box evaluation", "", f"Lead {lead} min, input window {window} min, embargo {window + lead} min. "
             "Skill = 1 - MAE / MAE of persistence (higher is better; 0 = no better than \"no change\").", ""]
    for field in dict.fromkeys(r[0] for r in rows):
        lines += [f"## {field}", "", "| rung | model | folds | MAE | RMSE | skill |", "|---|---|---|---|---|---|"]
        for rung in dict.fromkeys(r[1] for r in rows if r[0] == field):
            for model in dict.fromkeys(r[3] for r in rows if r[0] == field and r[1] == rung):
                sel = [r for r in rows if r[0] == field and r[1] == rung and r[3] == model]
                sk = [r[8] for r in sel if r[8] is not None]
                lines.append(f"| {rung} | {model} | {len(sel)} | {np.mean([r[6] for r in sel]):.3f} | "
                             f"{np.mean([r[7] for r in sel]):.3f} | {np.mean(sk) if sk else float('nan'):.3f} |")
        lines.append("")
    if lead_rows:
        with open(outdir / "lead_times.csv", "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["field", "method", "crossings", "warned_in_time", "missed", "false_alarms", "median_lead_min",
                        "test_days"])
            w.writerows(lead_rows)
        lines += ["## Early warning: how much earlier than the sensor", "",
                  "Replays the app's warning rule on the chronological test minutes (rung0). A warning is raised when "
                  "the path reaches the threshold within 30 min for 2 minutes in a row; it is confirmed when the sensor "
                  "crosses, a false alarm when nothing happens within 40 min.", "",
                  "| pollutant | method | crossings | warned in time | missed | false alarms | median lead (min) | test days |",
                  "|---|---|---|---|---|---|---|---|"]
        lines += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | {r[5]} | {r[6]} | {r[7]} |" for r in lead_rows]
        lines.append("")
    (outdir / "summary.md").write_text("\n".join(lines))
    print(f"\nWritten: {outdir / 'summary.md'}, metrics.csv, physics.json, dataset.csv")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default=str(ROOT / "config.yaml"))
    ap.add_argument("--db", help="database (default: the one in config.yaml)")
    ap.add_argument("--demo", action="store_true", help="simulated lab days instead of the database")
    ap.add_argument("--fields", nargs="+", default=["co2", "pm2_5", "pm10", "pm1", "voc"])
    ap.add_argument("--lead", type=int, default=10)
    ap.add_argument("--window", type=int, default=30)
    ap.add_argument("--epochs", type=int, default=30, help="BiLSTM epochs (0 = no neural models)")
    ap.add_argument("--out", default=str(ROOT / "research_out"))
    ap.add_argument("--max-folds", type=int, default=None, help="at most this many folds per rung (faster)")
    ap.add_argument("--demo-zones", action="store_true", help="simulated lab with a near and a far work zone")
    args = ap.parse_args()
    volume, default_ach, mu, zone = 300.0, 1.0, 1.0, None
    thresholds = {}
    if args.demo_zones:
        data = demo_zones_data()
        thresholds = {"pm2_5": (50.0, 40.0)}
    elif not args.demo:
        from iaqcam.config import load_config
        cfg = load_config(args.config)
        ph = cfg.get("physics") or {}
        volume = float(ph.get("volume_m3", volume))
        default_ach = float(ph.get("default_ach", default_ach))
        mu = float(ph.get("physics_weight", mu))
        zone = cfg.get("sensor_zone")
        inside = sorted({c for z in cfg["zones"] if z["inside"] for c in z["cameras"]})
        data = load_db(args.db or cfg["database"]["path"], zone, inside)
        thresholds = {k: (float(v["start"]), float(v["stop"])) for k, v in
                      ((cfg["fan"]["ai"].get("pollutants") or {}).items()) if isinstance(v, dict) and "start" in v}
    else:
        data = demo_data()
    fields = [f for f in args.fields if f in data] or args.fields
    run(data, fields, args.lead, args.window, args.epochs, volume, default_ach, mu, Path(args.out),
        args.max_folds, thresholds)


if __name__ == "__main__":
    main()
