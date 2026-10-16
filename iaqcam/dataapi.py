"""JSON for the dashboard: outlook, pollutant tiles, the chart, forecast error, tables."""
import math
from datetime import datetime, timedelta
from pathlib import Path

from flask import abort, jsonify, request

from .analytics import motion_index
from .db import FIELDS
from .sensor_meta import POLLUTANTS, band_note, canonical, classify, describe, thresholds

FMT = "%Y-%m-%d %H:%M:%S"
MAX_POINTS = 480            # longer ranges are averaged into wider steps


def _floor_minute(dt):
    return dt.replace(second=0, microsecond=0)


def time_range(args):
    """(start, end, live) from ?minutes=N or ?from=YYYY-MM-DDTHH:MM&to=..."""
    if args.get("from") and args.get("to"):
        try:
            start = datetime.strptime(args["from"][:16], "%Y-%m-%dT%H:%M")
            end = datetime.strptime(args["to"][:16], "%Y-%m-%dT%H:%M")
        except ValueError:
            abort(400)
        if end <= start or end - start > timedelta(days=31):
            abort(400)
        return start, end, False
    try:
        minutes = min(7 * 1440, max(10, int(args.get("minutes", 60))))
    except ValueError:
        abort(400)
    end = datetime.now()
    return end - timedelta(minutes=minutes), end, True


def register(app, cfg, state, query, fan=None, roi=None):
    zone_names = {z["key"]: z["name"] for z in cfg["zones"]}
    lead = int(cfg["forecast"]["lead_minutes"])
    index_ref = float(cfg["analytics"]["index_ref"])
    limits = (cfg["fan"]["ai"].get("pollutants") or {}) if cfg["fan"]["enabled"] else {}
    stale_after = float(cfg["fan"]["ai"]["stale_seconds"])

    # ------------------------------------------------------------- helpers
    hidden = {canonical(k) for k in (cfg["dashboard"].get("hide_fields") or [])}

    def fields():
        """Sensors shown on the dashboard (dashboard.hide_fields are left out everywhere)."""
        names = {r["field"]: r["name"] for r in query("SELECT field, name FROM sensor_fields")}
        out = []
        for f in FIELDS:
            if names.get(f) and canonical(names[f]) not in hidden:
                out.append({"key": f, "name": names[f], **describe(names[f])})
        return out

    def pollutant_fields(all_fields):
        by_kind = {f["kind"]: f for f in all_fields}
        tiles = [by_kind[k] for k in POLLUTANTS if k in by_kind]
        return tiles or all_fields[:5]          # channel without recognisable names

    def sensor_tiles(all_fields):
        """Every sensor of the channel: pollutants first, then temperature, humidity and the rest."""
        first = pollutant_fields(all_fields)
        keys = {f["key"] for f in first}
        rank = {"temp": 0, "hum": 1}
        rest = sorted((f for f in all_fields if f["key"] not in keys), key=lambda f: (rank.get(f["kind"], 2), f["key"]))
        return first + rest

    def people_rows(scope, start, end):
        """{minute: row} of persons / motion for one scope."""
        kind, _, key = scope.partition(":")
        a, b = start.strftime(FMT), end.strftime(FMT)
        # same names for every scope: occupancy, moving, total motion (BH/s), its temporal
        # variance, per-person mean / variance of the activity, IDs, speed
        cols = ("window_start AS t, n_person, n_person_mean, n_moving, motion_total, motion_var, "
                "mu_person, sigma2_person, unique_persons, mu_speed")
        if kind == "inside" or kind == "door":
            rows, label = query(f"SELECT {cols} FROM inside_minute WHERE window_start BETWEEN ? AND ?",
                                (a, b)), "inside the lab, all inside zones"
        elif kind == "zone" and key in zone_names:
            rows, label = query(f"SELECT {cols} FROM zone_minute WHERE zone = ? AND window_start "
                                "BETWEEN ? AND ?", (key, a, b)), zone_names[key]
        elif kind == "camera" and key.isdigit():
            rows = query("SELECT window_start AS t, n_person, n_person_mean, n_moving_mean AS n_moving, "
                         "motion_total, motion_total_var AS motion_var, mu_person, sigma2_person, "
                         "unique_persons, mu_speed, n_samples, n_valid "
                         "FROM camera_minute WHERE camera = ? AND window_start BETWEEN ? AND ?",
                         (int(key), a, b))
            label = f"camera {key}"
        else:
            abort(400)
        return {r["t"]: r for r in rows}, label

    def door_rows(start, end):
        rows = query("SELECT window_start AS t, n_in, n_out, inside_end FROM door_minute "
                     "WHERE window_start BETWEEN ? AND ?", (start.strftime(FMT), end.strftime(FMT)))
        return {r["t"]: r for r in rows}

    def model_note(model, field):
        """Badge text and explanation of how the forecast of one sensor is made right now."""
        if not model:
            return {"badge": "No forecast yet", "level": "wait",
                    "text": "The forecast starts once enough minutes of sensor readings are stored. "
                            + str(state.extras()[1].get("status") or "")}
        unit = field["unit"]
        errs = (f"On the test minutes the average miss was {model['mae_persistence']:.2f} {unit} for "
                f"\"no change\", {model['mae_sensors']:.2f} {unit} using the sensor's own history")
        if model["mae_full"] is not None:
            errs += f" and {model['mae_full']:.2f} {unit} when persons and motion are added"
        errs += f" ({model['n_samples']} minutes)."
        travel = ""
        if model["driver"]:
            travel = (f" Travel time: the reading follows {model['driver']} by about "
                      f"{model['lag_minutes']} min (link strength {model['lag_corr']:.2f}).")
        phys = ""
        if model.get("removal_per_hour") is not None:
            label = "air changes" if field["kind"] == "co2" else "removal (air change + deposition)"
            phys = (f" Physics: the air relaxes to {model['background']:.1f} {unit}; {label} "
                    f"{model['removal_per_hour']:.2f} per hour with the fan off"
                    + (f", {model['removal_fan_per_hour']:.2f} with the fan on" if model.get("removal_fan_per_hour") is not None else "")
                    + (f" ({model['n_decays']} decay periods)." if model.get("removal_source") == "decay"
                       else " (assumed: no decay of the empty room measured yet).")
                    + (f" Live air change while occupied: {model['lambda_online_per_hour']:.2f} per hour."
                       if model.get("lambda_online_per_hour") is not None else "")
                    + (f" CO2 per person from the camera count: {model['co2_gen_lpm']:.2f} L/min (adults at rest: about 0.3)."
                       if model.get("co2_gen_lpm") is not None else ""))
            if model.get("mae_physics") is not None:
                phys += (f" Test error: mass balance only {model['mae_physics']:.2f}, grey-box {model['mae_greybox']:.2f}, "
                         f"physics-informed loss {model['mae_physreg']:.2f} {unit}.")
        errs += phys
        named = {"physics": ("Physics forecast (mass balance)", "The forecast integrates the room's mass balance "
                             "10 minutes ahead: what the cameras' persons and motion emit, minus what the air "
                             "change removes."),
                 "greybox": ("Grey-box forecast: physics + learned rest", "The mass balance gives the forecast; a "
                             "regression on the recent readings, persons, motion and the fan state learns only what "
                             "the physics misses."),
                 "transit": ("Pollution in transit (camera zones + travel times)", "Every camera zone is a source with "
                             "a learned travel time to the sensor; the forecast integrates what is already on its way plus the "
                             "current activity (Early warning page)."),
                 "physreg": ("Physics-informed forecast", "A regression on the recent readings, persons, motion and the "
                             "fan state, trained to agree with the measured changes and with the mass balance.")}
        if model["used"] in named:
            badge, text = named[model["used"]]
            return {"badge": badge, "level": "ok", "text": text + " " + errs + travel}
        if model["used"] == "full":
            lead_text = (f" · {model['driver']} leads by about {model['lag_minutes']} min"
                         if model["driver"] else "")
            return {"badge": "Forecast uses persons and motion" + lead_text, "level": "ok",
                    "text": "The forecast combines the sensor's recent readings with persons, total motion, "
                            "its variation and walking speed over the past 30 minutes. " + errs + travel}
        if model["used"] == "sensors":
            return {"badge": "Forecast from the sensor's own history", "level": "note",
                    "text": "Persons and motion do not improve this forecast yet, or have not been "
                            "recorded long enough together with the sensors. " + errs + travel}
        if model["used"] == "paused":
            return {"badge": "Forecast paused = current reading", "level": "warn",
                    "text": "The model's recent forecasts were worse than \"no change\", so the value "
                            "in 10 minutes is shown as the reading now. It returns by itself when its "
                            "forecasts are better again. " + errs + travel}
        return {"badge": "Forecast = current reading", "level": "warn",
                "text": "No model beats \"no change\" on the stored minutes yet, so the value in 10 "
                        "minutes is shown as the reading now and the dashed line is the measured line "
                        "drawn 10 minutes later. This is the honest state of the model, not a fault. "
                        + errs + travel}

    # ------------------------------------------------------------ overview
    @app.get("/api/overview")
    def overview():
        start, end, live = time_range(request.args)
        all_fields = fields()
        tiles = sensor_tiles(all_fields)
        now = datetime.now()
        a = (end - timedelta(minutes=75)).strftime(FMT)
        b = end.strftime(FMT)
        sens = query("SELECT * FROM sensor_minute WHERE window_start BETWEEN ? AND ? "
                     "ORDER BY window_start", (a, b))
        latest = sens[-1] if sens else None
        latest_t = datetime.strptime(latest["window_start"], FMT) if latest else None
        fc_rows = query("SELECT field, predicted_for, predicted_at, value FROM forecasts "
                        "WHERE predicted_for > ? AND predicted_for <= ? ORDER BY predicted_for",
                        (a, (end + timedelta(minutes=lead + 1)).strftime(FMT)))
        models = {r["field"]: r for r in query("SELECT * FROM forecast_models")}

        out_tiles = []
        for f in tiles:
            key = f["key"]
            series = [(r["window_start"], r[key]) for r in sens if r[key] is not None]
            value = series[-1][1] if series else None
            value_t = series[-1][0] if series else None
            before = None
            if value_t:
                ref = (datetime.strptime(value_t, FMT) - timedelta(minutes=10)).strftime(FMT)
                older = [v for t, v in series if t <= ref]
                before = older[-1] if older else None
            future = [(r["predicted_for"], r["value"]) for r in fc_rows
                      if r["field"] == key and value_t and r["predicted_for"] > value_t]
            forecast = future[-1] if future else None
            band = classify(f["name"], value)
            band_fc = classify(f["name"], forecast[1]) if forecast else None
            raw = query(f"SELECT ts_local FROM sensor_readings WHERE {key} IS NOT NULL ORDER BY entry_id DESC LIMIT 1")
            age = (now - datetime.strptime(raw[0]["ts_local"], FMT)).total_seconds() if raw else None
            lim = limits.get(f["kind"])
            fan_on = (fan.status().get("requested") if fan else None)
            alert = None
            if lim and value is not None and live:
                alert = ("above" if value >= lim["start"] else
                         "recovering" if value > lim["stop"] and fan_on else "normal")
            out_tiles.append({
                **f, "value": value, "time": value_t, "band": band,
                "delta10": (value - before) if value is not None and before is not None else None,
                "forecast": forecast[1] if forecast else None,
                "forecast_for": forecast[0] if forecast else None,
                "forecast_band": band_fc,
                "forecast_delta": (forecast[1] - value) if forecast and value is not None else None,
                "used": (models.get(key) or {}).get("used"),
                "reading_ts": raw[0]["ts_local"] if raw else None, "age": age,
                "fresh": None if age is None else age <= stale_after,
                "fan_limits": lim, "alert": alert, "bands": thresholds(f["name"]),
                "spark": [[t, v] for t, v in series[-60:]],
                "spark_forecast": [[t, v] for t, v in future],
            })

        # outlook: the worst category now and in the forecast, and the pollutant to watch
        def worst(key):
            rated = [t for t in out_tiles if t[key]]
            return max(rated, key=lambda t: t[key]["level"]) if rated else None
        now_tile, next_tile = worst("band"), worst("forecast_band")
        outlook = {"now": None, "next": None, "watch": None, "tag": None, "warn": False,
                   "now_time": value_time(out_tiles), "next_time": None}
        if now_tile:
            outlook["now"] = now_tile["band"]
        if next_tile:
            outlook["next"] = next_tile["forecast_band"]
            outlook["next_time"] = next_tile["forecast_for"]
            candidates = [t for t in out_tiles if t["forecast_band"]
                          and t["forecast_band"]["level"] == next_tile["forecast_band"]["level"]]
            watch = max(candidates, key=lambda t: (t["forecast_delta"] or 0) / max(abs(t["value"] or 1), 1e-9))
            outlook["watch"] = {k: watch[k] for k in ("label", "unit", "digits", "value", "forecast", "key")}
            outlook["watch"]["band"] = watch["forecast_band"]
            if outlook["now"]:
                up = next_tile["forecast_band"]["level"] - outlook["now"]["level"]
                rel = (watch["forecast_delta"] or 0) / max(abs(watch["value"] or 1), 1e-9)
                if up > 0:
                    outlook["tag"], outlook["warn"] = "Air quality is expected to get worse", True
                elif up < 0:
                    outlook["tag"] = "Air quality is expected to improve"
                elif rel > 0.05:
                    outlook["tag"] = "Rising within the same category"
                elif rel < -0.05:
                    outlook["tag"] = "Falling within the same category"
                else:
                    outlook["tag"] = "No change expected"

        # context of the forecast: persons, motion, door traffic in the newest stored minute
        people, _ = people_rows("inside", end - timedelta(minutes=5), end)
        last_people = people[max(people)] if people else None
        doors = door_rows(end - timedelta(minutes=10), end)
        cam_snapshot, info, sensors_state = state.snapshot()
        door_state, forecast_state = state.extras()
        context = {
            "minute": last_people["t"] if last_people else None,
            "persons": last_people["n_person"] if last_people else None,
            "moving": last_people["n_moving"] if last_people else None,
            "motion": last_people["motion_total"] if last_people else None,
            "index": motion_index(last_people["motion_total"], index_ref) if last_people else None,
            "sigma2": last_people["motion_var"] if last_people else None,
            "ids": last_people["unique_persons"] if last_people else None,
            "speed": last_people["mu_speed"] if last_people else None,
            "door": {"enabled": bool(door_state.get("enabled")),
                     "in10": sum(r["n_in"] or 0 for r in doors.values()),
                     "out10": sum(r["n_out"] or 0 for r in doors.values()),
                     "inside": door_state.get("inside")} if door_state.get("enabled") else None,
        }

        # health pills
        newest = query("SELECT ts_local FROM sensor_readings ORDER BY entry_id DESC LIMIT 1")
        sensor_age = (now - datetime.strptime(newest[0]["ts_local"], FMT)).total_seconds() if newest else None
        ambient = {}
        for f in all_fields:
            if f["kind"] in ("temp", "hum") and latest and latest[f["key"]] is not None:
                ambient[f["kind"]] = latest[f["key"]]
        made = [r["predicted_at"] for r in fc_rows if r["predicted_at"]]
        forecast_age = (now - datetime.strptime(max(made), FMT)).total_seconds() / 60 if made else None
        bounds = query("SELECT MIN(window_start) AS a, MAX(window_start) AS b FROM sensor_minute")
        return jsonify(
            now=now.strftime(FMT), live=live, lead=lead, tiles=out_tiles, outlook=outlook,
            context=context,
            health={"sensor_age": sensor_age, "ambient": ambient, "forecast_age": forecast_age,
                    "forecast_status": forecast_state.get("status"),
                    "sensor_error": sensors_state.get("last_error"),
                    "channel": sensors_state.get("channel_name")},
            database={"from": bounds[0]["a"] if bounds else None, "to": bounds[0]["b"] if bounds else None},
            band_note=band_note())

    def value_time(tiles):
        times = [t["time"] for t in tiles if t["time"]]
        return max(times) if times else None

    # --------------------------------------------------------------- chart
    @app.get("/api/chart")
    def chart():
        start, end, live = time_range(request.args)
        scope = request.args.get("scope", "inside")
        all_fields = fields()
        field = next((f for f in all_fields if f["key"] == request.args.get("field")), None)
        if field is None:
            tiles = pollutant_fields(all_fields)
            field = next((f for f in tiles if f["kind"] == "pm10"), tiles[0] if tiles else None)
        if field is None:
            return jsonify(field=None, rows=[], bands=[], scope=scope, lead=lead)
        key = field["key"]
        start_m, end_m = _floor_minute(start), _floor_minute(end)
        a, b = start_m.strftime(FMT), end_m.strftime(FMT)
        measured = {r["t"]: r["v"] for r in query(
            f"SELECT window_start AS t, {key} AS v FROM sensor_minute WHERE window_start BETWEEN ? AND ?",
            (a, b)) if r["v"] is not None}
        ahead = (end_m + timedelta(minutes=lead)).strftime(FMT)
        forecast = {r["t"]: r["v"] for r in query(
            "SELECT predicted_for AS t, value AS v FROM forecasts WHERE field = ? AND predicted_for "
            "BETWEEN ? AND ?", (key, a, ahead))}
        people, scope_label = people_rows(scope, start_m, end_m)
        doors = door_rows(start_m, end_m)

        total = int((end_m - start_m).total_seconds() // 60) + 1 + lead
        step = max(1, math.ceil(total / MAX_POINTS))
        rows = []
        for i in range(0, total, step):
            bucket = [(start_m + timedelta(minutes=i + j)).strftime(FMT) for j in range(step)]
            row = {"t": bucket[0]}

            def mean(source, pick=lambda v: v):
                vals = [pick(source[m]) for m in bucket if m in source]
                vals = [v for v in vals if v is not None]
                return sum(vals) / len(vals) if vals else None
            m_val, f_val = mean(measured), mean(forecast)
            if m_val is not None:
                row["measured"] = round(m_val, 3)
            if f_val is not None:
                row["forecast"] = round(f_val, 3)
            ppl = [people[m] for m in bucket if m in people]
            if ppl:
                row["persons"] = max(p["n_person"] or 0 for p in ppl)
                row["persons_mean"] = round(sum(p["n_person_mean"] or 0 for p in ppl) / len(ppl), 3)
                def avg(name, digits):
                    vals = [p[name] for p in ppl if p.get(name) is not None]
                    return round(sum(vals) / len(vals), digits) if vals else None
                # missing values stay missing (gaps), never 0
                for out_key, name, digits in (("moving", "n_moving", 3), ("motion", "motion_total", 5),
                                              ("sigma2", "motion_var", 8), ("mu_person", "mu_person", 5),
                                              ("sigma2_person", "sigma2_person", 8)):
                    v = avg(name, digits)
                    if v is not None:
                        row[out_key] = v
                if "motion" in row:
                    row["index"] = motion_index(row["motion"], index_ref)
                ids = [p["unique_persons"] for p in ppl if p.get("unique_persons") is not None]
                if ids:
                    row["ids"] = max(ids)
                speeds = [p["mu_speed"] for p in ppl if p.get("mu_speed") is not None]
                if speeds:
                    row["speed"] = round(sum(speeds) / len(speeds), 4)
                if "n_samples" in ppl[0]:
                    row["samples"] = sum(p["n_samples"] or 0 for p in ppl)
            drs = [doors[m] for m in bucket if m in doors]
            if drs:
                row["n_in"] = sum(d["n_in"] or 0 for d in drs)
                row["n_out"] = sum(d["n_out"] or 0 for d in drs)
                row["door_inside"] = drs[-1]["inside_end"]
                if scope == "door":
                    row["persons"] = row["door_inside"]
            if len(row) > 1:
                rows.append(row)
        if scope == "door":
            scope_label = "inside the lab by door count; motion from all inside zones"
        model = query("SELECT * FROM forecast_models WHERE field = ?", (key,))
        model = model[0] if model else None
        return jsonify(field=field, bands=thresholds(field["name"]), rows=rows, step=step, lead=lead,
                       index_ref=index_ref, fan_limits=limits.get(field["kind"]),
                       start=a, end=b, live=live, now=datetime.now().strftime(FMT), scope=scope,
                       scope_label=scope_label, model=model, note=model_note(model, field))

    # -------------------------------------------------------------- status
    @app.get("/api/status")
    def status():
        """Lab-wide numbers for the overview cards, active alerts and recent events."""
        now = datetime.now()
        cams, info, sensors_state = state.snapshot()
        door_state, forecast_state = state.extras()
        inside_zones = {z["key"] for z in cfg["zones"] if z["inside"]}
        cam_zone = {c: z["key"] for z in cfg["zones"] for c in z["cameras"]}
        online = [c for c in cams.values() if c.get("connected") and c.get("stream_ts")
                  and now.timestamp() - c["stream_ts"] < 15]
        inside_live = [c for c in online if cam_zone.get(c["id"]) in inside_zones and c.get("count") is not None]
        totals = [c.get("total") for c in inside_live if c.get("total") is not None]
        variances = [c["rolling"]["var"] for c in inside_live if (c.get("rolling") or {}).get("var") is not None]
        minute = query("SELECT * FROM inside_minute ORDER BY window_start DESC LIMIT 1")
        minute = minute[0] if minute else None
        live = {"persons": sum(c["count"] for c in inside_live) if inside_live else None,
                "moving": sum(c.get("moving") or 0 for c in inside_live) if inside_live else None,
                "motion": round(sum(totals), 5) if totals else None,
                "index": motion_index(sum(totals), index_ref) if totals else None,
                "variance_60s": round(sum(variances), 8) if variances else None,
                "cameras_inside": len(inside_live)}
        last = None
        if minute:
            last = {"t": minute["window_start"], "persons": minute["n_person"], "persons_mean": minute["n_person_mean"],
                    "moving": minute["n_moving"], "motion": minute["motion_total"],
                    "index": motion_index(minute["motion_total"], index_ref), "variance": minute["motion_var"],
                    "ids": minute["unique_persons"], "speed": minute["mu_speed"]}
        newest = query("SELECT ts_local FROM sensor_readings ORDER BY entry_id DESC LIMIT 1")
        sensor_ts = newest[0]["ts_local"] if newest else None
        sensor_age = (now - datetime.strptime(sensor_ts, FMT)).total_seconds() if sensor_ts else None

        alerts = []

        def alert(level, text, kind):
            alerts.append({"level": level, "text": text, "kind": kind})
        setup = roi.summary(list(cams)) if roi is not None else None
        if setup and setup["pending"]:
            alert("warning", f"{len(setup['pending'])} of {setup['total']} cameras wait for setup (not analysed): "
                  + ", ".join("C" + str(c) for c in setup["pending"]), "setup")
        tick = float(cfg["pipeline"]["tick_seconds"])
        took = info.get("round_seconds")
        if took is not None and took > 1.5 * tick:
            alert("warning", f"Each camera is analysed every {took:.1f} s instead of every {tick:g} s "
                  f"({60 / took:.0f} pictures per minute): a GPU, a smaller model or a smaller imgsz helps", "system")
        off = [c for c in cams.values() if c not in online]
        if off:
            alert("critical" if not online else "warning",
                  f"{len(off)} of {len(cams)} cameras offline: " + ", ".join(c["name"] for c in off), "camera")
        if sensor_age is None:
            alert("warning", "No sensor reading stored yet" + (f" ({sensors_state.get('last_error')})" if sensors_state.get("last_error") else ""), "sensor")
        elif sensor_age > stale_after:
            alert("critical" if sensor_age > 3 * stale_after else "warning",
                  f"Sensor data stale: last reading {sensor_ts[11:16]} ({sensor_age / 60:.0f} min ago)", "sensor")
        names = {r["field"]: r["name"] for r in query("SELECT field, name FROM sensor_fields")}
        latest = query("SELECT * FROM sensor_minute ORDER BY window_start DESC LIMIT 1")
        fc = {r["field"]: r["value"] for r in query(
            "SELECT field, value FROM forecasts WHERE predicted_for > ? ORDER BY predicted_for", (now.strftime(FMT),))}
        fan_st = fan.status() if fan else {}
        if latest and sensor_age is not None and sensor_age <= stale_after:
            for f in FIELDS:
                if not names.get(f) or latest[0][f] is None:
                    continue
                d, v = describe(names[f]), latest[0][f]
                band = classify(names[f], v)
                lim = limits.get(d["kind"])
                if band and band["level"] >= 3:
                    alert("critical", f"{d['label']} {v:.{d['digits']}f} {d['unit']}: {band['label']}", "pollutant")
                elif lim and v >= lim["start"]:
                    alert("warning", f"{d['label']} {v:.{d['digits']}f} {d['unit']} is above the fan start threshold {lim['start']:g}", "pollutant")
                if lim and fc.get(f) is not None and fc[f] >= lim["start"] and v < lim["start"]:
                    alert("warning", f"{d['label']} forecast {fc[f]:.{d['digits']}f} {d['unit']} in {lead} min would reach the start threshold {lim['start']:g}", "forecast")
        if fan:
            if fan_st.get("ok") is False:
                alert("critical", "Fan controller not reachable: " + str(fan_st.get("error") or "unknown error"), "fan")
            if fan_st.get("dry_run"):
                alert("info", "Fan control is in DRY RUN: no relay is switched", "fan")
            lc = fan_st.get("last_command") or {}
            if lc.get("ok") is False:
                alert("warning", f"Last fan command ({lc.get('state', '').upper()}) failed: {lc.get('error')}", "fan")
        if str(forecast_state.get("status") or "").startswith("Forecast failed"):
            alert("warning", forecast_state["status"], "forecast")
        for it in transit_rows():
            tt = it.get("ttt") or {}
            lim = it.get("threshold")
            if lim and it["value"] < lim["start"] and (tt.get("committed") or tt.get("expected")):
                fan_t = tt.get("fan_on")
                fan_txt = "" if "fan_on" not in it.get("paths", {}) else (
                    "; fan on now keeps it below" if fan_t is None else f"; with the fan on now in {fan_t} min")
                if tt.get("committed"):
                    alert("critical", f"Early warning: {it['label']} will reach {lim['start']:g} {it['unit']} in about "
                          f"{tt['committed']} min, already in the air{fan_txt}", "transit")
                else:
                    alert("warning", f"Early warning: {it['label']} expected to reach {lim['start']:g} {it['unit']} in "
                          f"about {tt['expected']} min as work goes on{fan_txt}", "transit")
        order = {"critical": 0, "warning": 1, "info": 2}
        alerts.sort(key=lambda a: order[a["level"]])
        events = query("SELECT ts, kind, level, message FROM events ORDER BY id DESC LIMIT 12")
        return jsonify(now=now.strftime(FMT), live=live, minute=last, index_ref=index_ref,
                       cameras={"online": len(online), "total": len(cams)},
                       sensors={"last": sensor_ts, "age": sensor_age, "stale_after": stale_after,
                                "error": sensors_state.get("last_error"), "channel": sensors_state.get("channel_name")},
                       health={"round_seconds": info.get("round_seconds"), "model": info.get("model"),
                               "device": info.get("device"), "forecast": forecast_state.get("status"),
                               "started": info.get("started")},
                       fan=fan_st if fan else None, alerts=alerts, events=events, lead=lead,
                       setup=setup)

    # ------------------------------------------------------ pollution in transit
    def transit_rows():
        import json as _json
        visible = {f["key"]: f for f in pollutant_fields(fields())}
        out = []
        for r in query("SELECT field, made_at, data FROM transit_now"):
            if r["field"] not in visible:
                continue
            try:
                d = _json.loads(r["data"])
            except ValueError:
                continue
            age = (datetime.now() - datetime.strptime(r["made_at"], FMT)).total_seconds()
            if age > 15 * 60:
                continue                                   # stale: the forecaster stopped
            out.append({**d, "field": r["field"], "made_at": r["made_at"], "order": list(visible).index(r["field"])})
        return sorted(out, key=lambda d: d["order"])

    @app.get("/api/transit")
    def transit_view():
        """Early warning: pollution in transit per pollutant (paths, sources, travel times),
        the measured last 30 minutes, and the record of warnings and their lead time."""
        items = transit_rows()
        since = (datetime.now() - timedelta(minutes=40)).strftime(FMT)
        for it in items:
            rows = query(f"SELECT window_start AS t, {it['field']} AS v FROM sensor_minute WHERE window_start >= ? "
                         f"AND {it['field']} IS NOT NULL ORDER BY window_start", (since,))
            it["recent"] = [[r["t"], r["v"]] for r in rows]
        days = 30
        a = (datetime.now() - timedelta(days=days)).strftime(FMT)
        ew = query("SELECT * FROM early_warnings WHERE COALESCE(warned_at, crossed_at) >= ? ORDER BY id DESC", (a,))
        leads = sorted(r["lead_min"] for r in ew if r["outcome"] == "confirmed" and r["lead_min"] is not None)
        score = {"days": days, "confirmed": sum(r["outcome"] == "confirmed" for r in ew),
                 "missed": sum(r["outcome"] == "missed" for r in ew),
                 "false_alarms": sum(r["outcome"] == "false alarm" for r in ew),
                 "open": sum(r["outcome"] == "open" for r in ew),
                 "median_lead": leads[len(leads) // 2] if leads else None}
        return jsonify(items=items, score=score, warnings=ew[:25], volume_m3=(cfg.get("physics") or {}).get("volume_m3"))

    # ------------------------------------------------------- physics of the air
    @app.get("/api/physics")
    def physics_view():
        """Mass balance of the room air: estimated constants, the forecast variants, the
        inputs (sensed now / future) and the research evaluation, if run."""
        ph = cfg.get("physics") or {}
        brief = request.args.get("brief") == "1"
        now = datetime.now()
        models = {r["field"]: r for r in query("SELECT * FROM forecast_models")}
        pols = []
        for f in pollutant_fields(fields()):
            m = models.get(f["key"])
            item = {**f, "model": m}
            if m and m.get("mae_persistence"):
                base = m["mae_persistence"]
                item["skill"] = {name: (None if m.get(col) is None else round(1 - m[col] / base, 3))
                                 for name, col in (("physics", "mae_physics"), ("data", "mae_full"),
                                                   ("sensors", "mae_sensors"), ("greybox", "mae_greybox"),
                                                   ("physreg", "mae_physreg"), ("transit", "mae_transit"))}
            pols.append(item)
        events = query("SELECT ts, kind, level, message, pollutant, details FROM events "
                       "WHERE kind IN ('physics', 'model') ORDER BY id DESC LIMIT ?", (1 if brief else 40,))
        out = {"events": events, "enabled": bool(ph.get("enabled", True)), "volume_m3": ph.get("volume_m3"),
               "volume_default": float(ph.get("volume_m3") or 0) == 300.0,
               "default_ach": ph.get("default_ach"), "lead": lead, "pollutants": pols}
        if brief:
            return jsonify(out)

        start, end, _ = time_range(request.args)
        a, b = start.strftime(FMT), end.strftime(FMT)
        series = {}
        for r in query("SELECT * FROM physics_minute WHERE window_start BETWEEN ? AND ? ORDER BY window_start", (a, b)):
            series.setdefault(r["field"], []).append(r)
        out["series"] = series

        # inputs of the mass balance: sensed now, configured, or future work
        day = (now - timedelta(days=1)).strftime(FMT)
        recent = (now - timedelta(minutes=15)).strftime(FMT)
        cams = query("SELECT COUNT(*) AS n FROM camera_minute WHERE window_start >= ?", (recent,))[0]["n"]
        ctx = query("SELECT fan_on, dry_run, window_start FROM context_minute ORDER BY window_start DESC LIMIT 1")
        door_n = query("SELECT COUNT(*) AS n FROM door_minute WHERE window_start >= ?", (day,))[0]["n"]
        mach = query("SELECT COUNT(DISTINCT machine) AS n, MAX(window_start) AS last FROM machine_minute "
                     "WHERE window_start >= ?", (day,))[0]
        co2 = next((p for p in pols if p["kind"] == "co2" and p["model"]), None)
        decays = sum((p["model"] or {}).get("n_decays") or 0 for p in pols)
        out["inputs"] = [
            {"name": "Persons", "term": "S (E_res), CO₂ N·G/V", "status": "live" if cams else "waiting",
             "detail": "camera head count inside the detection areas"},
            {"name": "Motion", "term": "S (E_res)", "status": "live" if cams else "waiting",
             "detail": "total body motion (BH/s) and its variance"},
            {"name": "Exhaust fan", "term": "λ", "status": ("dry run" if ctx and ctx[0]["dry_run"] else "live") if ctx else "waiting",
             "detail": "state every minute; counts for λ only when the relay is really switched"},
            {"name": "Door traffic", "term": "λ", "status": "live" if door_n else "not connected",
             "detail": "people in + out at the glass door (door counter)"},
            {"name": "Empty-room decays", "term": "λ (+ k_dep)", "status": "live" if decays else "waiting",
             "detail": f"{decays} decay periods measured" if decays else "needs the room empty for 15+ min with the value falling"},
            {"name": "Machines / enclosures", "term": "S (E_mach)", "status": "live" if mach["n"] else "future",
             "detail": (f"{mach['n']} machines reporting" if mach["n"] else
                        "laser cutters, 3D printers, extractors: POST /api/machines from a smart plug or agent")},
            {"name": "Windows", "term": "λ", "status": "future",
             "detail": "open / closed from the camera (boundary detection) or reed switches"},
            {"name": "Outdoor air", "term": "C_b", "status": "estimated",
             "detail": "lowest indoor levels of the history until an outdoor sensor exists"},
            {"name": "Room volume", "term": "V", "status": "check" if out["volume_default"] else "set",
             "detail": f"{ph.get('volume_m3')} m³ in config.yaml" + (" (placeholder: set the real value)" if out["volume_default"] else "")},
        ]
        if co2:
            out["co2_generation_lpm"] = co2["model"].get("co2_gen_lpm")

        # research evaluation (research/greybox_eval.py), if it has been run
        research = Path(cfg["base_dir"]) / "research_out"
        met = research / "metrics.csv"
        if met.exists():
            import csv as _csv
            agg = {}
            with open(met, newline="", encoding="utf-8") as fh:
                for r in _csv.DictReader(fh):
                    k = (r["field"], r["rung"], r["model"])
                    a_ = agg.setdefault(k, {"mae": [], "skill": []})
                    a_["mae"].append(float(r["mae"]))
                    if r.get("skill_vs_persistence") not in (None, "", "None"):
                        a_["skill"].append(float(r["skill_vs_persistence"]))
            out["research"] = {
                "updated": datetime.fromtimestamp(met.stat().st_mtime).strftime(FMT),
                "rows": [{"field": f_, "rung": rg, "model": md, "folds": len(v["mae"]),
                          "mae": round(sum(v["mae"]) / len(v["mae"]), 4),
                          "skill": round(sum(v["skill"]) / len(v["skill"]), 3) if v["skill"] else None}
                         for (f_, rg, md), v in agg.items()]}
            pj = research / "physics.json"
            if pj.exists():
                import json as _json
                try:
                    out["research"]["physics"] = _json.loads(pj.read_text(encoding="utf-8"))
                except ValueError:
                    pass
        return jsonify(out)

    # ------------------------------------------------------- forecast error
    @app.get("/api/errors")
    def errors():
        start, end, _ = time_range(request.args)
        a, b = start.strftime(FMT), end.strftime(FMT)
        out = []
        for f in pollutant_fields(fields()):
            key = f["key"]
            r = query(f"SELECT COUNT(*) AS n, AVG(ABS(f.value - s.{key})) AS err, "
                      f"AVG(ABS(f.base_value - s.{key})) AS naive, AVG(s.{key}) AS level "
                      f"FROM forecasts f JOIN sensor_minute s ON s.window_start = f.predicted_for "
                      f"WHERE f.field = ? AND s.{key} IS NOT NULL AND f.predicted_for BETWEEN ? AND ?",
                      (key, a, b))[0]
            level = r["level"] or 0
            out.append({**f, "n": r["n"], "error": r["err"], "naive": r["naive"], "level": level,
                        "percent": (100 * r["err"] / level) if r["n"] and level > 0 else None,
                        "naive_percent": (100 * r["naive"] / level) if r["n"] and level > 0 and r["naive"] is not None else None})
        return jsonify(items=out, lead=lead)

