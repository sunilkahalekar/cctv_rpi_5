"""
CCTV occupancy + motion + air quality: one command starts everything.

    python run.py                  # all cameras in config.yaml + ThingSpeak + dashboard
    python run.py --cameras 8      # only camera 8
    python run.py --test 8         # grab one frame from camera 8, save it and exit

Reads the NVR's RTSP streams as a viewer (nothing is changed on the NVR), detects
persons with YOLO, computes the motion score of iaq-edge-pipeline, and stores
count / mean / variance per camera per minute in a local SQLite database. A
second thread stores the ThingSpeak channel's sensor readings. The dashboard is
served on http://127.0.0.1:8001 by default.
"""
import os

# Must be set before cv2 is imported: RTSP over TCP avoids corrupted frames.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import argparse
import sys
import threading
from pathlib import Path

try:
    # On Windows torch must be loaded before cv2, otherwise torch's DLLs can fail to initialise.
    import torch  # noqa: F401
except Exception:
    pass

import cv2

from iaqcam.auth import UserStore
from iaqcam.cameras import CameraReader
from iaqcam.videocap import capture_factory
from iaqcam.config import camera_source, load_config, needs_password, zone_of_camera
from iaqcam.db import Database
from iaqcam.door import DoorCounter, DoorLine
from iaqcam.fan_service import FanService
from iaqcam.forecast import Forecaster
from iaqcam.pipeline import LiveState, Pipeline
from iaqcam.roi import RoiStore
from iaqcam.thingspeak import ThingSpeakPoller
from iaqcam.web import create_app

BASE = Path(__file__).resolve().parent


def test_camera(cfg, cam):
    cap = cv2.VideoCapture(camera_source(cfg, cam), cv2.CAP_FFMPEG)
    ok, frame = cap.read() if cap.isOpened() else (False, None)
    cap.release()
    if not ok:
        sys.exit(f"{cam['name']}: could not read a frame. Check IP, port, user/password and "
                 "channel number.")
    out = BASE / f"test_cam{cam['id']}.jpg"
    cv2.imwrite(str(out), frame)
    print(f"{cam['name']}: OK, {frame.shape[1]}x{frame.shape[0]} frame saved to {out}")


def manage_users(cfg, args):
    """Dashboard accounts. The password is typed here on the PC and only its hash is stored."""
    import getpass
    users = UserStore(cfg["dashboard"]["users_file"])
    if args.add_user:
        first = getpass.getpass(f"New password for {args.add_user}: ")
        if first != getpass.getpass("Type it again: "):
            sys.exit("The two passwords are not the same. Nothing was changed.")
        try:
            users.add(args.add_user, first)
        except ValueError as exc:
            sys.exit(f"Not saved: {exc}")
        print(f"User {args.add_user} saved. It works at once, also while the app is running.")
    if args.remove_user:
        print(f"User {args.remove_user} removed." if users.remove(args.remove_user)
              else f"There is no user {args.remove_user}.")
    if args.list_users:
        print("Dashboard users:", ", ".join(users.names()) or "(none)")


def setup_logs(cfg):
    """logs/fan.log always; logs/analytics_debug.log when analytics.debug_log is true."""
    import logging
    import logging.handlers
    folder = Path(cfg["log_dir"])
    folder.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    files = [("iaqcam.fan", "fan.log", True), ("iaqcam.analytics", "analytics_debug.log",
                                                bool(cfg["analytics"].get("debug_log")))]
    for name, filename, on in files:
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if on:
            handler = logging.handlers.RotatingFileHandler(folder / filename, maxBytes=5_000_000,
                                                           backupCount=3, encoding="utf-8")
            handler.setFormatter(fmt)
            logger.addHandler(handler)
        if name == "iaqcam.fan":
            console = logging.StreamHandler()
            console.setFormatter(logging.Formatter("  [fan] %(message)s"))
            logger.addHandler(console)


def calibrate(cfg):
    """Distribution of the stored motion values, to choose the thresholds (README)."""
    import numpy as np
    from iaqcam.db import read_connection
    con = read_connection(cfg["database"]["path"])
    try:
        acts = np.array([r[0] for r in con.execute(
            "SELECT flow_px * 1.0 FROM person_obs WHERE in_roi = 1 AND flow_px IS NOT NULL")], float)
        act = np.array([r[0] for r in con.execute(
            "SELECT activity FROM person_obs WHERE in_roi = 1 AND activity IS NOT NULL")], float)
        spd = np.array([r[0] for r in con.execute(
            "SELECT speed FROM person_obs WHERE in_roi = 1 AND speed IS NOT NULL")], float)
        tot = np.array([r[0] for r in con.execute(
            "SELECT motion_total FROM inside_minute WHERE motion_total IS NOT NULL")], float)
    finally:
        con.close()
    q = (5, 25, 50, 75, 90, 95, 99)

    def show(name, a, unit):
        if a.size == 0:
            print(f"{name}: no data yet")
            return
        print(f"{name} ({a.size} values, {unit}): " +
              "  ".join(f"p{k}={v:.4g}" for k, v in zip(q, np.percentile(a, q))))
    a = cfg["analytics"]
    print("Current settings: activity_noise", a["activity_noise"], " speed_noise", a["speed_noise"],
          " index_ref", a["index_ref"])
    show("Optical flow in person boxes", acts, "pixels")
    show("Activity per person (after noise filter)", act, "BH/s")
    show("Walking speed per person (after noise filter)", spd, "BH/s")
    show("Total motion inside the lab per minute", tot, "BH/s")
    if tot.size:
        print(f"Suggestion: index_ref = {np.percentile(tot, 90):.3g}  (a busy minute, 90th percentile, shows as 63)")
    print("For activity_noise / speed_noise: record a few minutes when people sit still with "
          "analytics.debug_log: true and take a value just above what those persons show.")


def lan_addresses():
    """IPv4 addresses of this PC on the local network (no traffic is sent)."""
    import socket
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        found += socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        pass
    return [a for i, a in enumerate(found) if not a.startswith("127.") and a not in found[:i]]


def dashboard_urls(host, port):
    if host in ("0.0.0.0", ""):
        return [f"http://127.0.0.1:{port}"] + [f"http://{a}:{port}" for a in lan_addresses()]
    return [f"http://{host}:{port}"]


def model_label(cfg, section="model"):
    """File name of the model a section runs (.hef with the Hailo backend, else the weights)."""
    m = cfg[section]
    if str(cfg["model"].get("backend") or "ultralytics").lower() == "hailo":
        hef = m.get("hef") if m.get("hef") and Path(str(m["hef"])).is_file() else cfg["model"].get("hef")
        return Path(str(hef)).name if hef else "(no hef)"
    return Path(str(m["weights"])).name


def main():
    parser = argparse.ArgumentParser(description="CCTV person/motion features + ThingSpeak + dashboard")
    parser.add_argument("--config", default=str(BASE / "config.yaml"))
    parser.add_argument("--cameras", type=int, nargs="+", metavar="ID",
                        help="use only these camera ids from the config")
    parser.add_argument("--test", type=int, metavar="ID", help="grab one frame from this camera and exit")
    parser.add_argument("--no-thingspeak", action="store_true", help="do not read the ThingSpeak channel")
    parser.add_argument("--no-door", action="store_true", help="do not run the door in/out counter")
    parser.add_argument("--add-user", metavar="NAME", help="create a dashboard user (or change the password) and exit")
    parser.add_argument("--remove-user", metavar="NAME", help="delete a dashboard user and exit")
    parser.add_argument("--list-users", action="store_true", help="show the dashboard users and exit")
    parser.add_argument("--calibrate", action="store_true", help="show the distribution of the stored motion values and exit")
    args = parser.parse_args()

    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        sys.exit(str(exc))
    if args.add_user or args.remove_user or args.list_users:
        return manage_users(cfg, args)
    if args.calibrate:
        return calibrate(cfg)
    wanted = [args.test] if args.test else args.cameras
    if wanted:
        cfg["cameras"] = [c for c in cfg["cameras"] if c["id"] in wanted]
    if not cfg["cameras"]:
        sys.exit("No cameras selected. Check the 'cameras' list in config.yaml.")
    if needs_password(cfg):
        sys.exit("Set the NVR password first:  set NVR_PASSWORD=your_password")
    if args.test:
        return test_camera(cfg, cfg["cameras"][0])

    Path(cfg["database"]["path"]).parent.mkdir(parents=True, exist_ok=True)
    db = Database(cfg["database"]["path"])
    p = cfg["pipeline"]
    open_capture = capture_factory(p)
    if str(p.get("decoder")).lower() == "ffmpeg":
        print(f"Video: ffmpeg, {p['decode_fps']} pictures/s per camera"
              f"{', width ' + str(p['decode_width']) if p.get('decode_width') else ''}, hardware decoding: {p['hwaccel']}")
    cameras = [CameraReader(c["id"], c.get("name") or f"Camera {c['id']}", camera_source(cfg, c),
                            p["tick_seconds"], p["flow_width"], str(c.get("location") or ""),
                            p["flow_interval"], open_capture)
               for c in cfg["cameras"]]
    cam_zone = zone_of_camera(cfg)
    db.write_layout(((c.cam_id, c.cam_name, c.location, cam_zone.get(c.cam_id)) for c in cameras),
                    cfg["zones"])
    state = LiveState(cameras)
    setup_logs(cfg)
    for cam in cameras:
        cam.events = (lambda cid: lambda message, level: db.write_event("camera", message, level, camera=cid))(cam.cam_id)
    db.write_event("system", f"app started: {len(cameras)} camera(s), model {model_label(cfg)}")
    st = cfg["setup"]
    roi = RoiStore(cfg["roi_file"], cfg.get("roi"), st["require_setup"], st["crop_margin"], st["head_room"])
    progress = roi.summary([c.cam_id for c in cameras])
    names = {"area": "detection area", "full": "whole picture", "skip": "not analysed", "setup": "WAITING FOR SETUP"}
    print(f"Camera setup: {progress['done']} of {progress['total']} cameras set up")
    for c in cameras:
        print(f"  C{c.cam_id:<3} {c.cam_name:<12} {names[progress['modes'][c.cam_id]]}")
    if progress["pending"]:
        print("  Cameras waiting for setup are NOT analysed. Open the dashboard -> Setup, draw the detection\n"
              "  area of each camera and save: detection starts for that camera right away.")

    print("Loading the YOLO model ...")
    try:
        pipeline = Pipeline(cfg, cameras, db, state, roi)
    except ValueError as exc:                       # model classes do not match
        sys.exit(f"Model {model_label(cfg)}: {exc}\nCheck model.backend / model.hef / model.classes in the config.")
    except RuntimeError as exc:                     # AI HAT+ 2 busy or not reachable
        db.write_event("error", f"model not loaded: {exc}", "critical")
        sys.exit(f"Model {model_label(cfg)}: {exc}")
    print(f"Model {pipeline.model_name} at {pipeline.imgsz}px on "
          f"{pipeline.accelerator}, {len(cameras)} camera(s); {pipeline.class_note}; "
          f"YOLO looks {'around the detection areas only' if pipeline.crop_to_area else 'at the whole picture'}")

    for cam in cameras:
        cam.start()

    workers = []
    if cfg["thingspeak"]["enabled"] and cfg["thingspeak"]["channel_id"] and not args.no_thingspeak:
        workers.append(ThingSpeakPoller(cfg, db, state))
        if cfg["forecast"]["enabled"]:
            workers.append(Forecaster(cfg, db, state))

    door_line = door_counter = None
    door_reader = next((c for c in cameras if c.cam_id == cfg["door"]["camera"]), None)
    if cfg["door"]["enabled"] and door_reader is not None and not args.no_door:
        door_line = DoorLine(cfg["door"]["line_file"], cfg["door"]["line"], cfg["door"]["inside_point"])
        door_counter = DoorCounter(cfg, door_reader, db, state, door_line)
        workers.append(door_counter)
        print(f"Door counter on {door_reader.cam_name} ({model_label(cfg, 'door')}, "
              f"{cfg['door']['fps']} frames per second while someone is moving)")
    fan = None
    if cfg["fan"]["enabled"]:
        fan = FanService(cfg, db)
        workers.append(fan)
        print(f"Exhaust fan: {'DRY RUN (no relay)' if fan.dry_run else cfg['fan']['controller'] + ' controller'}, {fan.mode} mode, operators: "
              f"{', '.join(cfg['fan']['operators']) or '(none listed: nobody can switch it)'}")
    for worker in workers:
        worker.start()

    dash = cfg["dashboard"]
    app = create_app(cfg, state, door_line, door_counter, roi, fan, db)
    threading.Thread(target=lambda: app.run(host=dash["host"], port=int(dash["port"]),
                                            threaded=True, use_reloader=False),
                     daemon=True, name="dashboard").start()
    urls = dashboard_urls(dash["host"], int(dash["port"]))
    state.update_info(urls=urls[1:])
    print(f"Dashboard: {urls[0]}   (Ctrl+C to stop)")
    for url in urls[1:]:
        print(f"  from other PCs on the network: {url}")
    if len(urls) > 1:
        print("  If other PCs cannot open it, allow Python through Windows Firewall (see README).")
    print(f"Database : {cfg['database']['path']}")
    if dash["require_login"] and not UserStore(dash["users_file"]).names():
        print("No dashboard user exists yet. In a second window run:  python run.py --add-user NAME")

    try:
        pipeline.run()
    except KeyboardInterrupt:
        print("\nStopping ...")
    except Exception as exc:
        db.write_event("error", f"app stopped by an unexpected error: {type(exc).__name__}: {exc}", "critical")
        raise
    finally:
        db.write_event("system", "app stopped")
        pipeline.stop()
        for worker in workers:
            worker.stop()
        for cam in cameras:
            cam.stop()
        if door_counter is not None:
            door_counter.join(timeout=5)      # lets it write its last minute row
        pipeline.flush()
        if fan is not None:
            fan.shutdown()
        db.close()


if __name__ == "__main__":
    main()
