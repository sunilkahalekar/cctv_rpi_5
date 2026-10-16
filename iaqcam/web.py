"""Dashboard: Flask app serving the page, JSON for the charts and the camera pictures."""
import csv
import hmac
import io
import logging
import os
import time
from datetime import datetime, timedelta

from pathlib import Path
from urllib.parse import quote

from flask import (Flask, Response, abort, jsonify, redirect, render_template, request,
                   send_file, session)

from .auth import Throttle, UserStore, secret_key
from .config import zone_of_camera
from . import dataapi
from .analytics import motion_index
from .db import read_connection

OFFLINE_AFTER_SECONDS = 15


def create_app(cfg, state, door_line=None, door_counter=None, roi=None, fan=None, db=None):
    app = Flask(__name__)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    db_path = cfg["database"]["path"]
    show_video = bool(cfg["dashboard"]["show_video"])
    zones = cfg["zones"]
    cam_zone = zone_of_camera(cfg)
    zone_names = {z["key"]: z["name"] for z in zones}

    def query(sql, args=()):
        con = read_connection(db_path)
        try:
            return [dict(r) for r in con.execute(sql, args).fetchall()]
        finally:
            con.close()

    # ---------------------------------------------------------------- sign-in
    dash = cfg["dashboard"]
    require_login = bool(dash["require_login"])
    users = UserStore(dash["users_file"])
    throttle = Throttle()
    app.secret_key = secret_key(dash["secret_file"])
    app.config.update(SESSION_COOKIE_NAME="iaqcam_session", SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax",
                      PERMANENT_SESSION_LIFETIME=timedelta(hours=float(dash["session_hours"])))
    logos = [p for p in dash["logos"] if Path(p).is_file()]
    before, mark, after = str(dash["title"]).partition(str(dash.get("title_highlight") or "Air-Quality"))
    title_parts = (before, mark, after)

    def signed_in():
        return bool(session.get("user")) and session["user"] in users.names()

    operators = set(cfg["fan"]["operators"])
    index_ref = float(cfg["analytics"]["index_ref"])

    def may_operate():
        """Fan control and detection areas: user names listed in fan.operators. Without
        sign-in only this PC itself may change them."""
        if require_login:
            return signed_in() and session["user"] in operators
        return request.remote_addr in ("127.0.0.1", "::1")

    def operator_refusal():
        who = session.get("user") if require_login else request.remote_addr
        return jsonify(ok=False, error=f"{who} is not allowed to change this. Operators are listed under "
                                       "fan: operators: in config.yaml."), 403

    def json_body():
        if not request.is_json:              # a plain web form from another site cannot send JSON
            abort(415)
        return request.get_json(silent=True) or {}

    def safe_next(value):
        value = value or "/"
        return value if value.startswith("/") and not value.startswith("//") and "\\" not in value else "/"

    machine_token = os.environ.get(str((cfg.get("physics") or {}).get("machine_token_env") or "MACHINE_AGENT_TOKEN"), "")

    def machine_agent():
        """A machine agent / smart plug authenticated by its token (no browser session)."""
        auth = request.headers.get("Authorization", "")
        return bool(machine_token) and hmac.compare_digest(auth, "Bearer " + machine_token)

    @app.before_request
    def guard():
        path = request.path
        if not require_login or path == "/login" or path.startswith("/brand/") or signed_in():
            return None
        if path == "/api/machines" and request.method == "POST" and machine_agent():
            return None
        if path.startswith(("/api/", "/frame/", "/export/", "/static/")):
            return jsonify(error="sign in first"), 401
        return redirect("/login?next=" + quote(path) if path != "/" else "/login")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        target = safe_next(request.values.get("next"))
        if not require_login or signed_in():
            return redirect(target)
        error, username, status = None, "", 200
        who = request.remote_addr or "?"
        if request.method == "POST":
            username = (request.form.get("username") or "").strip()[:40]
            if throttle.blocked(who):
                error, status = "Too many attempts. Wait five minutes and try again.", 429
            elif users.verify(username, request.form.get("password") or ""):
                throttle.clear(who)
                session.clear()
                session["user"] = username
                session.permanent = True
                return redirect(target)
            else:
                throttle.failed(who)
                time.sleep(0.4)
                error, status = "User name or password is not correct.", 401
        no_users = not users.names()
        return render_template("login.html", title=dash["title"], subtitle=dash["subtitle"],
                               title_parts=title_parts, footer=dash["footer"],
                               contact=dash["contact"], logo_count=len(logos), next=target,
                               username=username, error=error or no_users,
                               no_users=no_users and not error), status

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.get("/brand/<int:n>")
    def brand(n):
        if not 0 <= n < len(logos):
            abort(404)
        return send_file(logos[n], max_age=3600)

    @app.get("/")
    def index():
        return render_template("index.html", show_video=show_video, title=dash["title"],
                               subtitle=dash["subtitle"], title_parts=title_parts,
                               user=session.get("user") if require_login else None,
                               logo_count=len(logos),
                               channel_id=cfg["thingspeak"]["channel_id"],
                               sensors_enabled=bool(cfg["thingspeak"]["enabled"]),
                               sensor_zone=zone_names.get(cfg.get("sensor_zone") or "", ""))

    # ------------------------------------------------------------------ live
    @app.get("/api/live")
    def live():
        cameras, info, sensors = state.snapshot()
        door, forecast = state.extras()
        now = time.time()
        cams = {}
        for cam in cameras.values():
            # Online = the stream is delivering frames. Analysis may lag behind when a
            # round over all cameras takes longer than a few seconds.
            stream = cam.pop("stream_ts")
            analysed = cam.pop("last_ts")
            cam["online"] = bool(cam.pop("connected") and stream
                                 and now - stream < OFFLINE_AFTER_SECONDS)
            cam["last_seen"] = datetime.fromtimestamp(stream).strftime("%H:%M:%S") if stream else None
            cam["analysed"] = datetime.fromtimestamp(analysed).strftime("%H:%M:%S") if analysed else None
            cam["zone"] = cam_zone.get(cam["id"])
            cam["setup"] = roi.mode(cam["id"]) if roi is not None else "full"
            if cam["setup"] not in ("area", "full"):
                cam["count"] = cam["detected"] = cam["moving"] = None
                cam["people"] = []
            if not cam["online"]:
                cam["count"] = None
            cams[cam["id"]] = cam

        zone_out = []
        for z in zones:
            members = [cams[c] for c in z["cameras"] if c in cams]
            if not members:
                continue
            seen = [c for c in members if c["online"] and c["count"] is not None]
            minutes = [c["minute"] for c in members if c.get("minute")]
            minute = None
            if minutes:
                latest = max(m["window_start"] for m in minutes)
                same = [m for m in minutes if m["window_start"] == latest]
                totals = [m["motion_total"] for m in same if m.get("motion_total") is not None]
                variances = [m["motion_total_var"] for m in same if m.get("motion_total_var") is not None]
                total = round(sum(totals), 5) if totals else None
                minute = {"window_start": latest, "cameras": len(same),
                          "n_person": sum(m["n_person"] for m in same),
                          "n_moving": round(sum(m.get("n_moving_mean") or 0 for m in same), 2),
                          "motion_total": total, "motion_index": motion_index(total, index_ref),
                          "motion_total_var": round(sum(variances), 8) if variances else None}
            zone_out.append({"key": z["key"], "name": z["name"], "inside": z["inside"],
                             "cameras": [c["id"] for c in members],
                             "online": sum(1 for c in members if c["online"]),
                             "count": sum(c["count"] for c in seen) if seen else None,
                             "minute": minute})
        for z in zone_out:
            seen = [cams[c] for c in z["cameras"] if cams[c]["online"] and cams[c]["count"] is not None]
            z["moving"] = sum(c.get("moving") or 0 for c in seen) if seen else None
        inside = [z["count"] for z in zone_out if z["inside"] and z["count"] is not None]
        outside = [z["count"] for z in zone_out if not z["inside"] and z["count"] is not None]
        return jsonify(now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       cameras=list(cams.values()), zones=zone_out,
                       inside_now=sum(inside) if inside else None,
                       outside_now=sum(outside) if outside else None,
                       cameras_online=sum(1 for c in cams.values() if c["online"]),
                       info=info, sensors=sensors, door=door, forecast=forecast,
                       operator=may_operate(), user=session.get("user") if require_login else None,
                       index_ref=index_ref, fan_enabled=fan is not None,
                       setup=roi.summary(list(cams)) if roi is not None else None)

    # history, outlook, tiles, chart, forecast error: see dataapi.py
    dataapi.register(app, cfg, state, query, fan, roi)

    # ---------------------------------------------------------------- events
    @app.get("/api/events")
    def events_list():
        """Event log plus every door crossing, newest first.
        Time: ?day=YYYY-MM-DD, ?days=N (last N days), ?from=&to= or ?minutes=; nothing = everything."""
        a = request.args
        F = dataapi.FMT
        start = end = None
        if a.get("day"):
            try:
                start = datetime.strptime(a["day"][:10], "%Y-%m-%d")
            except ValueError:
                abort(400)
            end = start + timedelta(days=1)
        elif a.get("days", "").isdigit():
            end = datetime.now() + timedelta(minutes=1)
            start = (end - timedelta(days=int(a["days"]))).replace(hour=0, minute=0, second=0, microsecond=0)
        elif a.get("from") or a.get("minutes"):
            start, end, _ = dataapi.time_range(a)
            end += timedelta(minutes=1)
        kinds_wanted = [v for v in a.getlist("kind") if v]
        limit = min(5000, max(1, a.get("limit", 1000, type=int)))

        where, args = [], []
        if start:
            where.append("ts >= ? AND ts < ?")
            args += [start.strftime(F), end.strftime(F)]
        for name, col in (("level", "level"), ("pollutant", "pollutant"), ("fan_state", "fan_state")):
            values = [v for v in a.getlist(name) if v]
            if values:
                where.append(f"{col} IN ({', '.join('?' * len(values))})")
                args += values
        ev_kinds = [k for k in kinds_wanted if k != "door"]
        if ev_kinds:
            where.append(f"kind IN ({', '.join('?' * len(ev_kinds))})")
            args += ev_kinds
        if a.get("camera", "").isdigit():
            where.append("camera = ?")
            args.append(int(a["camera"]))
        if a.get("roi"):
            where.append("roi LIKE ?")
            args.append("%" + a["roi"][:40] + "%")
        if a.get("q"):
            where.append("(message LIKE ? OR details LIKE ?)")
            args += ["%" + a["q"][:80] + "%"] * 2
        rows = []
        if not kinds_wanted or ev_kinds:
            rows = query("SELECT id, ts, kind, level, message, camera, roi, pollutant, fan_state, mode, ok, user, details "
                         "FROM events" + (" WHERE " + " AND ".join(where) if where else "") +
                         " ORDER BY ts DESC, id DESC LIMIT ?", tuple(args) + (limit,))

        # door crossings are events too (stored in door_events by the door counter)
        door_ok = (not kinds_wanted or "door" in kinds_wanted) and not any(
            a.get(k) for k in ("level", "pollutant", "fan_state", "roi"))
        if door_ok:
            dw, dargs = [], []
            if start:
                dw.append("ts >= ? AND ts < ?")
                dargs += [start.strftime(F), end.strftime(F)]
            if a.get("camera", "").isdigit():
                dw.append("camera = ?")
                dargs.append(int(a["camera"]))
            for r in query("SELECT id, ts, camera, direction, track_id FROM door_events" +
                           (" WHERE " + " AND ".join(dw) if dw else "") + " ORDER BY ts DESC LIMIT ?",
                           tuple(dargs) + (limit,)):
                msg = f"Lab door: a person walked {'IN' if r['direction'] == 'in' else 'OUT'}"
                if a.get("q") and a["q"].lower() not in msg.lower():
                    continue
                rows.append({"id": "d" + str(r["id"]), "ts": r["ts"], "kind": "door", "level": "info", "message": msg,
                             "camera": r["camera"], "roi": None, "pollutant": None, "fan_state": None, "mode": None,
                             "ok": None, "user": None, "details": None, "direction": r["direction"]})
        rows.sort(key=lambda r: r["ts"], reverse=True)
        total = len(rows)
        counts = {}
        for r in rows:
            counts[r["kind"]] = counts.get(r["kind"], 0) + 1
        rows = rows[:limit]

        # days that have events (for the day picker), newest first
        per_day = {}
        for r in query("SELECT substr(ts, 1, 10) AS d, COUNT(*) AS n FROM events GROUP BY d") + \
                query("SELECT substr(ts, 1, 10) AS d, COUNT(*) AS n FROM door_events GROUP BY d"):
            per_day[r["d"]] = per_day.get(r["d"], 0) + r["n"]
        days = [{"day": d, "n": per_day[d]} for d in sorted(per_day, reverse=True)[:31]]
        kinds = sorted({r["kind"] for r in query("SELECT DISTINCT kind FROM events")} | {"door"})
        return jsonify(events=rows, kinds=kinds, counts=counts, total=total, limit=limit, days=days,
                       start=start.strftime(F) if start else None, end=end.strftime(F) if end else None)

    # ------------------------------------------------------- spatial spread
    @app.get("/api/positions")
    def positions():
        cam = request.args.get("camera", type=int)
        cams, _, _ = state.snapshot()
        if cam not in cams:
            abort(400)
        size = cams[cam].get("frame_size")
        start, end, _ = dataapi.time_range(request.args)
        nx, ny = 32, 18
        grid = [[0] * nx for _ in range(ny)]
        total = 0
        if size:
            w, h = size
            for r in query("SELECT (x1 + x2) / 2.0 AS fx, y2 AS fy, moving FROM person_obs WHERE camera = ? AND "
                           "in_roi = 1 AND Timestamp BETWEEN ? AND ?",
                           (cam, start.strftime(dataapi.FMT), end.strftime(dataapi.FMT))):
                i, j = min(ny - 1, max(0, int(r["fy"] / h * ny))), min(nx - 1, max(0, int(r["fx"] / w * nx)))
                grid[i][j] += 1
                total += 1
        return jsonify(camera=cam, nx=nx, ny=ny, grid=grid, total=total, frame_size=size)

    # ---------------------------------------------------------- diagnostics
    @app.get("/api/diagnostics")
    def diagnostics():
        """Raw and filtered per-person values, per-picture totals and a recomputation of the
        stored minutes from the stored pictures (backend vs displayed values)."""
        if not may_operate():
            return operator_refusal()
        cam = request.args.get("camera", type=int)
        minutes = min(30, max(1, request.args.get("minutes", 5, type=int)))
        since = (datetime.now() - timedelta(minutes=minutes)).strftime(dataapi.FMT)
        persons = query("SELECT Timestamp AS ts, global_id % 1000000 AS pid, conf, in_roi, flow_px, activity_raw, activity, "
                        "speed_raw, speed, moving, "
                        "y2 - y1 AS height FROM person_obs WHERE camera = ? AND Timestamp >= ? ORDER BY person_obs.id DESC LIMIT 400",
                        (cam, since))
        pictures = query("SELECT Timestamp AS ts, n_detected, Person_Count AS in_area, n_moving, motion_total, "
                         "Motion_Score AS frame_score_px FROM camera_second WHERE camera = ? AND Timestamp >= ? "
                         "ORDER BY id DESC LIMIT 200", (cam, since))
        stored = query("SELECT window_start, n_samples, n_valid, n_person, n_person_mean, n_moving_mean, motion_total, "
                       "motion_total_var, motion_index, mu_person, sigma2_person, mu_motion, sigma2_motion "
                       "FROM camera_minute WHERE camera = ? AND window_start >= ? ORDER BY window_start DESC",
                       (cam, (datetime.now() - timedelta(minutes=minutes + 1)).strftime(dataapi.FMT)))
        checks = []
        for m in stored:
            end = (datetime.strptime(m["window_start"], dataapi.FMT) + timedelta(seconds=60)).strftime(dataapi.FMT)
            vals = [r["motion_total"] for r in query(
                "SELECT motion_total FROM camera_second WHERE camera = ? AND Timestamp >= ? AND Timestamp < ? "
                "AND motion_total IS NOT NULL", (cam, m["window_start"], end))]
            mean = sum(vals) / len(vals) if vals else None
            var = sum((v - mean) ** 2 for v in vals) / len(vals) if len(vals) >= 2 else None
            same = (mean is None) == (m["motion_total"] is None) and (mean is None or abs(mean - m["motion_total"]) < 1e-4) \
                and (var is None) == (m["motion_total_var"] is None) and (var is None or abs(var - m["motion_total_var"]) < 1e-6)
            checks.append({"window_start": m["window_start"], "pictures": len(vals), "recomputed_mean": mean,
                           "recomputed_var": var, "stored_mean": m["motion_total"], "stored_var": m["motion_total_var"],
                           "match": bool(same)})
        a = cfg["analytics"]
        cams, _, _ = state.snapshot()
        live_cam = cams.get(cam) or {}
        return jsonify(camera=cam, persons=persons, pictures=pictures, minutes=stored, checks=checks,
                       live={k: live_cam.get(k) for k in ("count", "detected", "moving", "total", "index", "rolling", "flow_dt", "frame_size")},
                       settings={"activity_noise": a["activity_noise"], "speed_noise": a["speed_noise"],
                                 "index_ref": a["index_ref"], "flow_interval": cfg["pipeline"]["flow_interval"],
                                 "window_seconds": cfg["pipeline"]["window_seconds"],
                                 "max_gap_seconds": cfg["tracking"]["max_gap_seconds"],
                                 "reid_seconds": cfg["tracking"]["reid_seconds"]})

    # ------------------------------------------------------------------ door
    @app.get("/api/door")
    def door_get():
        if door_line is None:
            return jsonify(enabled=False)
        line, inside_point = door_line.get()
        return jsonify(enabled=True, camera=cfg["door"]["camera"], line=line, inside_point=inside_point)

    @app.post("/api/door/line")
    def door_set_line():
        if door_line is None:
            abort(404)
        data = json_body()
        try:
            door_line.set(data["line"], data["inside_point"])
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify(ok=False, error=str(exc)), 400
        return jsonify(ok=True)

    @app.post("/api/door/inside")
    def door_set_inside():
        if door_counter is None:
            abort(404)
        if not request.is_json:
            abort(415)
        data = request.get_json(silent=True) or {}
        try:
            value = int(data["value"])
            if not 0 <= value <= 5000:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            return jsonify(ok=False, error="value must be a whole number from 0 to 5000"), 400
        door_counter.set_inside(value)
        return jsonify(ok=True)

    # ------------------------------------------------------- detection areas
    @app.get("/api/roi/<int:cam>")
    def roi_get(cam):
        if roi is None:
            abort(404)
        cameras, _, _ = state.snapshot()
        if cam not in cameras:
            abort(404)
        st = roi.setup(cam)
        return jsonify(camera=cam, polygons=st["areas"], mode=st["mode"], configured=st["configured"],
                       updated=st["updated"], by=st["by"], operator=may_operate())

    @app.get("/api/setup")
    def setup_overview():
        if roi is None:
            abort(404)
        cameras, info, _ = state.snapshot()
        out = []
        for cid, c in sorted(cameras.items()):
            st = roi.setup(cid)
            out.append({"id": cid, "name": c["name"], "location": c["location"], "mode": st["mode"],
                        "areas": sum(1 for a in st["areas"] if a["enabled"]), "updated": st["updated"],
                        "by": st["by"], "online": bool(c.get("connected")), "frame_seq": c.get("frame_seq"),
                        "frame_size": c.get("frame_size")})
        return jsonify(cameras=out, summary=roi.summary(list(cameras)), require_setup=roi.require_setup,
                       detect_in=cfg["setup"]["detect_in"], crop_per_area=bool(cfg["setup"]["crop_per_area"]), crop_margin=roi.crop_margin, head_room=roi.head_room,
                       model=info.get("model"), classes=info.get("classes"),
                       operator=may_operate())

    def roi_event(cam, text, st):
        user = session.get("user") or request.remote_addr
        logging.getLogger("iaqcam.analytics").info("setup of camera %s changed by %s: %s", cam, user, st)
        if db is not None:
            db.write_event("roi", text, camera=cam, roi=", ".join(p["name"] for p in st["areas"] if p["enabled"]) or None,
                           user=user, details={"mode": st["mode"], "areas": st["areas"]})

    @app.put("/api/roi/<int:cam>")
    def roi_put(cam):
        if roi is None:
            abort(404)
        data = json_body()
        if not may_operate():
            return operator_refusal()
        cameras, _, _ = state.snapshot()
        if cam not in cameras:
            abort(404)
        before = roi.mode(cam)
        try:
            st = roi.set(cam, data.get("polygons"), data.get("mode"), session.get("user"))
        except (TypeError, ValueError) as exc:
            return jsonify(ok=False, error=str(exc)), 400
        what = {"area": "detection inside " + (", ".join(p["name"] for p in st["areas"] if p["enabled"])),
                "full": "detection in the whole picture", "skip": "not analysed"}[st["mode"]]
        roi_event(cam, f"camera {cam} {'set up' if before == 'setup' else 'setup changed'}: {what}", st)
        return jsonify(ok=True, polygons=st["areas"], mode=st["mode"], configured=True,
                       updated=st["updated"], by=st["by"])

    @app.delete("/api/roi/<int:cam>")
    def roi_reset(cam):
        if roi is None:
            abort(404)
        json_body()
        if not may_operate():
            return operator_refusal()
        cameras, _, _ = state.snapshot()
        if cam not in cameras:
            abort(404)
        old = roi.setup(cam)
        roi.reset(cam)
        roi_event(cam, f"camera {cam} setup removed: detection stopped until it is set up again", old)
        return jsonify(ok=True, mode=roi.mode(cam))

    # ------------------------------------------------- machines (E_mach, future work)
    @app.post("/api/machines")
    def machines_post():
        """Machine / enclosure states for the mass balance, e.g. from a smart plug or an agent:
        {"machines": [{"machine": "laser1", "kind": "machine", "on": true, "power_w": 850}]}
        Allowed for an operator signed in, or with  Authorization: Bearer <MACHINE_AGENT_TOKEN>."""
        if db is None:
            abort(404)
        data = json_body()
        if not (machine_agent() or may_operate()):
            return jsonify(ok=False, error="operator sign-in or machine agent token needed"), 403
        items = data.get("machines")
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            return jsonify(ok=False, error="'machines' must be a list of 1 to 50 objects"), 400
        minute = datetime.now().strftime("%Y-%m-%d %H:%M:00")
        rows = []
        for it in items:
            if not isinstance(it, dict) or not str(it.get("machine") or "").strip():
                return jsonify(ok=False, error="every item needs a 'machine' name"), 400
            name = str(it["machine"]).strip()[:40]
            kind = str(it.get("kind") or "machine")[:20]
            on = it.get("on")
            if not isinstance(on, bool):
                return jsonify(ok=False, error=f"{name}: 'on' must be true or false"), 400
            power = it.get("power_w")
            if power is not None:
                try:
                    power = float(power)
                except (TypeError, ValueError):
                    return jsonify(ok=False, error=f"{name}: 'power_w' must be a number"), 400
            rows.append((minute, name, kind, int(on), power))
        db.write_machine_minute(rows)
        return jsonify(ok=True, minute=minute, stored=len(rows))

    @app.get("/api/machines")
    def machines_get():
        rows = query("SELECT machine, kind, on_state, power_w, MAX(window_start) AS last FROM machine_minute "
                     "GROUP BY machine ORDER BY machine")
        return jsonify(machines=rows)

    # --------------------------------------------------------- experiment log
    EXP_TYPES = {"decay_off": "Empty-room decay, fan off", "decay_on": "Empty-room decay, fan on",
                 "co2_count": "CO2 per person (known head count, door closed)",
                 "source_near": "Particle source near the sensor", "source_far": "Particle source at a far bench",
                 "fan_step": "Fan step (switch on during work)", "count_check": "Camera count check (manual tally)",
                 "baseline": "Normal lab day (baseline)", "other": "Other"}

    def exp_active():
        import json as _json
        try:
            return _json.loads(db.get_state("exp_active") or "null")
        except ValueError:
            return None

    @app.get("/api/experiments")
    def experiments_get():
        """Experiment log for the paper: what was done in the lab and when."""
        if db is None:
            abort(404)
        rows = query("SELECT ts, level, message, user, details FROM events WHERE kind = 'experiment' "
                     "ORDER BY id DESC LIMIT 200")
        return jsonify(active=exp_active(), types=EXP_TYPES, operator=may_operate(), events=rows)

    @app.post("/api/experiments")
    def experiments_post():
        """{"action": "start", "type": "decay_off", "note": "..."} | {"action": "stop", "note": ...}
        | {"action": "note", "note": "..."} (operators only)."""
        import json as _json
        if db is None:
            abort(404)
        data = json_body()
        if not may_operate():
            return operator_refusal()
        action = data.get("action")
        note = str(data.get("note") or "").strip()[:300]
        who = session.get("user") or request.remote_addr
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        act = exp_active()
        if action == "start":
            typ = data.get("type")
            if typ not in EXP_TYPES:
                return jsonify(ok=False, error="unknown experiment type"), 400
            if act:
                return jsonify(ok=False, error=f"'{EXP_TYPES.get(act['type'], act['type'])}' is still running: stop it first"), 409
            act = {"id": now, "type": typ, "start": now, "note": note, "user": who}
            db.set_state("exp_active", _json.dumps(act))
            db.write_event("experiment", f"Experiment started: {EXP_TYPES[typ]}" + (f" ({note})" if note else ""),
                           user=who, details={"action": "start", **act})
        elif action == "stop":
            if not act:
                return jsonify(ok=False, error="no experiment is running"), 409
            db.set_state("exp_active", "null")
            mins = (datetime.now() - datetime.strptime(act["start"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60
            db.write_event("experiment", f"Experiment ended: {EXP_TYPES.get(act['type'], act['type'])} after "
                           f"{mins:.0f} min" + (f" ({note})" if note else ""), user=who,
                           details={"action": "stop", **act, "end": now, "end_note": note})
            act = None
        elif action == "note":
            if not note:
                return jsonify(ok=False, error="write a note first"), 400
            db.write_event("experiment", "Lab note: " + note, user=who,
                           details={"action": "note", "note": note, "during": act["id"] if act else None})
        else:
            return jsonify(ok=False, error="action must be start, stop or note"), 400
        return jsonify(ok=True, active=act)

    # ------------------------------------------------------------ exhaust fan
    @app.get("/api/fan")
    def fan_get():
        if fan is None:
            return jsonify(enabled=False)
        events = query("SELECT ts, kind, level, message, fan_state, mode, ok, user FROM events "
                       "WHERE kind IN ('fan_mode', 'fan_command', 'fan_decision', 'fan_error', 'controller') "
                       "ORDER BY id DESC LIMIT 30")
        return jsonify(enabled=True, status=fan.status(), policy=fan.describe_policy(),
                       operator=may_operate(), events=events)

    @app.post("/api/fan/mode")
    def fan_mode():
        if fan is None:
            abort(404)
        data = json_body()
        if not may_operate():
            return operator_refusal()
        ok, msg = fan.set_mode(str(data.get("mode")), session.get("user") or request.remote_addr)
        return jsonify(ok=ok, message=msg, status=fan.status()), (200 if ok else 400)

    @app.post("/api/fan/command")
    def fan_command():
        if fan is None:
            abort(404)
        data = json_body()
        if not may_operate():
            return operator_refusal()
        if data.get("state") not in ("on", "off"):
            return jsonify(ok=False, error="state must be on or off"), 400
        ok, msg = fan.manual_command(data["state"] == "on", session.get("user") or request.remote_addr)
        return jsonify(ok=ok, message=msg, error=None if ok else msg, status=fan.status()), (200 if ok else 409)

    @app.post("/api/fan/controller")
    def fan_controller_select():
        """{"name": "pi" | "esp32"}: which agent switches the fan (fan: controller: dual)."""
        if fan is None:
            abort(404)
        data = json_body()
        if not may_operate():
            return operator_refusal()
        if data.get("name") not in ("pi", "esp32"):
            return jsonify(ok=False, error="name must be pi or esp32"), 400
        ok, msg = fan.select_controller(data["name"], session.get("user") or request.remote_addr)
        return jsonify(ok=ok, message=msg, error=None if ok else msg, status=fan.status()), (200 if ok else 409)

    # --------------------------------------------------------------- pictures
    @app.get("/frame/<key>.jpg")
    def frame(key):
        if not show_video:
            abort(404)
        seq = request.args.get("seq", type=int)
        data = state.get_jpeg("door" if key == "door" else int(key) if key.isdigit() else None, seq)
        if data is None:
            abort(404)
        return Response(data, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- exports
    def csv_response(rows, filename):
        buf = io.StringIO()
        if rows:
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={filename}"})

    EXPORTS = {
        "camera_minute": "SELECT * FROM camera_minute WHERE window_start BETWEEN ? AND ? ORDER BY window_start, camera",
        "person_obs": "SELECT * FROM person_obs WHERE Timestamp BETWEEN ? AND ? ORDER BY id",
        "events": "SELECT * FROM events WHERE ts BETWEEN ? AND ? ORDER BY ts",
        "zone_minute": "SELECT * FROM zone_minute WHERE window_start BETWEEN ? AND ? ORDER BY window_start, zone",
        "inside_minute": "SELECT * FROM inside_minute WHERE window_start BETWEEN ? AND ? ORDER BY window_start",
        "door_minute": "SELECT * FROM door_minute WHERE window_start BETWEEN ? AND ? ORDER BY window_start",
        "door_events": "SELECT * FROM door_events WHERE ts BETWEEN ? AND ? ORDER BY id",
        "sensor_readings": "SELECT * FROM sensor_readings WHERE ts_local BETWEEN ? AND ? ORDER BY entry_id",
        "forecasts": "SELECT * FROM forecasts WHERE predicted_for BETWEEN ? AND ? ORDER BY predicted_for, field",
    }

    @app.get("/export/<name>.csv")
    def export(name):
        if name not in EXPORTS:
            abort(404)
        start, end, _ = dataapi.time_range(request.args)
        end += timedelta(minutes=cfg["forecast"]["lead_minutes"] + 1)      # include the forecasts ahead
        rows = query(EXPORTS[name], (start.strftime(dataapi.FMT), end.strftime(dataapi.FMT)))
        return csv_response(rows, f"{name}.csv")

    return app
