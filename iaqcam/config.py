"""Load config.yaml, apply environment overrides, build camera sources and zones."""
import os
from pathlib import Path
from urllib.parse import quote

import yaml

DEFAULTS = {
    "nvr": {"ip": "", "port": 554, "user": "admin", "password": "", "stream_index": 1,
            "url_template": "rtsp://{user}:{password}@{ip}:{port}/Media/Live/Normal"
                            "?camera=C_{channel}&streamindex={stream}"},
    "cameras": [],
    "zones": [],
    "sensor_zone": None,
    "model": {"backend": "ultralytics", "weights": "yolo26m.pt", "imgsz": 1280, "conf": 0.25,
              "device": None, "batch": 0, "classes": ["person"],
              "hef": None, "labels": "coco", "timeout_ms": 10000},
    "setup": {"require_setup": True, "detect_in": "area", "crop_per_area": True, "crop_margin": 0.04,
              "head_room": 0.35},
    "pipeline": {"tick_seconds": 1.0, "window_seconds": 60, "flow_width": 640, "flow_interval": 0.12,
                 "keep_seconds_days": 7, "decoder": "opencv", "hwaccel": "auto", "decode_fps": 10,
                 "decode_width": 0},
    "tracking": {"max_gap_seconds": 20, "reid_seconds": 120, "hold_seconds": 90, "exit_hold_seconds": 5,
                 "edge_hold_seconds": 30, "min_hits": 2, "conf_low": 0.10, "confirm_conf": 0.5,
                 "walk_speed": 0.3},
    "analytics": {"activity_noise": 0.005, "speed_noise": 0.05, "index_ref": 0.3,
                  "show_outside": True, "debug_log": False},
    "fan": {"enabled": False, "dry_run": True, "controller": "remote", "operators": [], "startup_mode": "last",
            "check_seconds": 15, "default_mode": "manual",
            "remote": {"url": "", "token_env": "FAN_AGENT_TOKEN", "token_file": "fan_agent_token.txt",
                       "timeout_seconds": 3, "retries": 2},
            "esp32": {"url": "", "token_env": "ESP32_AGENT_TOKEN", "token_file": "esp32_agent_token.txt",
                      "timeout_seconds": 3, "retries": 1, "pin": 26, "active_high": True},
            "preferred": "pi", "failover": "auto",
            "gpio": {"pin": 9, "active_high": True, "feedback_pin": None},
            "ai": {"pollutants": {}, "activation_seconds": 120, "forecast_trigger": True,
                   "forecast_confirmations": 3, "forecast_requires_model": True,
                   "recovery_seconds": 300, "min_on_seconds": 600, "min_off_seconds": 180,
                   "stale_seconds": 300, "stale_action": "off"}},
    "door": {"enabled": False, "camera": None, "line": [[0.3, 0.6], [0.7, 0.6]],
             "inside_point": [0.5, 0.3], "weights": "yolo26n.pt", "imgsz": 640, "conf": 0.3,
             "fps": 5, "reset_time": "04:00", "classes": ["person"], "hef": None, "labels": None},
    "forecast": {"enabled": True, "lead_minutes": 10, "min_samples": 180, "history_days": 14,
                 "fields": ["pm1", "pm2_5", "pm10", "co2", "voc"]},
    "physics": {"enabled": True, "volume_m3": 300.0, "default_ach": 1.0, "physics_weight": 1.0,
                "machine_token_env": "MACHINE_AGENT_TOKEN"},
    "database": {"path": "iaq_cctv.db"},
    "thingspeak": {"enabled": True, "channel_id": None, "read_api_key": "", "poll_seconds": 20,
                   "backfill_results": 2000, "field_names": {},
                   "base_url": "https://api.thingspeak.com"},
    "dashboard": {"host": "127.0.0.1", "port": 8001, "show_video": True, "video_width": 1280,
                  "title": "Lab Occupancy and Air-Quality Intelligence",
                  "subtitle": "CCTV vision and air sensing",
                  "require_login": True, "session_hours": 12, "contact": "", "logos": [],
                  "footer": ["Runs on this PC", "Data stays in the lab"], "hide_fields": ["rawvoc"]},
}


def _resolve(base, value):
    p = Path(str(value))
    return str(p if p.is_absolute() else base / p)


def load_config(path):
    path = Path(path).resolve()
    with open(path, encoding="utf-8") as fh:
        user_cfg = yaml.safe_load(fh) or {}
    cfg = {}
    for section, default in DEFAULTS.items():
        value = user_cfg.get(section)
        if isinstance(default, dict):
            cfg[section] = {**default, **(value or {})}
        else:
            cfg[section] = value if value is not None else default
    cfg["base_dir"] = path.parent
    for key in ("remote", "esp32", "gpio"):            # second level of the fan section
        cfg["fan"][key] = {**DEFAULTS["fan"][key], **(cfg["fan"].get(key) or {})}
        if cfg["fan"][key].get("token_file"):
            cfg["fan"][key]["token_file"] = _resolve(path.parent, cfg["fan"][key]["token_file"])

    nvr = cfg["nvr"]
    nvr["user"] = os.environ.get("NVR_USER") or nvr["user"]
    nvr["password"] = os.environ.get("NVR_PASSWORD") or nvr["password"] or ""

    cfg["model"]["weights"] = _resolve(path.parent, cfg["model"]["weights"])
    cfg["door"]["weights"] = _resolve(path.parent, cfg["door"]["weights"])
    for sec in ("model", "door"):                      # .hef and labels files of the Hailo backend
        if cfg[sec].get("hef"):
            cfg[sec]["hef"] = _resolve(path.parent, cfg[sec]["hef"])
        lab = cfg[sec].get("labels")
        if lab and str(lab).lower() != "coco":
            cfg[sec]["labels"] = _resolve(path.parent, lab)
    validate_engine(cfg)
    cfg["door"]["line_file"] = str(path.parent / "door_line.json")
    cfg["roi_file"] = str(path.parent / "roi.json")
    cfg["log_dir"] = str(path.parent / "logs")
    validate_fan(cfg)
    validate_setup(cfg)
    cfg["database"]["path"] = _resolve(path.parent, cfg["database"]["path"])
    dash = cfg["dashboard"]
    dash["users_file"] = str(path.parent / "users.json")
    dash["secret_file"] = str(path.parent / "dashboard_secret.key")
    dash["logos"] = [_resolve(path.parent, p) for p in (dash.get("logos") or [])][:3]
    cfg["zones"] = build_zones(cfg)
    return cfg


def build_zones(cfg):
    """Normalised zone list; every camera ends up in exactly one zone."""
    cam_ids = [c["id"] for c in cfg["cameras"]]
    zones, taken = [], set()
    for i, z in enumerate(cfg.get("zones") or []):
        cams = [c for c in (z.get("cameras") or []) if c in cam_ids and c not in taken]
        taken.update(cams)
        zones.append({"key": str(z.get("key") or f"zone{i + 1}"),
                      "name": str(z.get("name") or z.get("key") or f"Zone {i + 1}"),
                      "inside": bool(z.get("inside", True)), "cameras": cams, "sort": i})
    rest = [c for c in cam_ids if c not in taken]
    if rest:
        if zones:
            print(f"Cameras {rest} are not listed in any zone in config.yaml; "
                  "they are shown as 'Unassigned' and not counted as inside the lab.")
            zones.append({"key": "unassigned", "name": "Unassigned", "inside": False,
                          "cameras": rest, "sort": len(zones)})
        else:
            zones.append({"key": "lab", "name": "Lab", "inside": True, "cameras": rest, "sort": 0})
    return zones


def zone_of_camera(cfg):
    return {cam: z["key"] for z in cfg["zones"] for cam in z["cameras"]}


def camera_source(cfg, cam):
    """RTSP URL (or explicit source) for one camera entry."""
    if cam.get("source") not in (None, ""):
        return cam["source"]
    nvr = cfg["nvr"]
    return nvr["url_template"].format(
        user=quote(str(nvr["user"]), safe=""), password=quote(str(nvr["password"]), safe=""),
        ip=nvr["ip"], port=nvr["port"], channel=cam["id"],
        stream=cam.get("stream_index", nvr["stream_index"]))


def needs_password(cfg):
    return any(c.get("source") in (None, "") for c in cfg["cameras"]) and not cfg["nvr"]["password"]


def validate_fan(cfg):
    """Merge the nested fan settings with their defaults and check them; raises ValueError."""
    fan = cfg["fan"]
    d = DEFAULTS["fan"]
    for key in ("remote", "gpio", "ai"):
        fan[key] = {**d[key], **(fan.get(key) or {})}
    errors = []
    fan["esp32"] = {**d["esp32"], **(fan.get("esp32") or {})}
    if fan["controller"] not in ("mock", "gpio", "remote", "dual", "esp32"):
        errors.append("fan.controller must be mock, gpio, remote, dual or esp32")
    if fan["enabled"] and not fan["dry_run"] and fan["controller"] == "esp32" and not fan["esp32"]["url"]:
        errors.append("fan.controller esp32 needs fan.esp32.url (address of the ESP32 fan agent)")
    if fan.get("preferred") not in ("pi", "esp32"):
        errors.append("fan.preferred must be pi or esp32")
    if fan.get("failover") not in ("auto", "manual"):
        errors.append("fan.failover must be auto or manual")
    if fan["enabled"] and not fan["dry_run"] and fan["controller"] == "dual" and not (fan["remote"]["url"] and fan["esp32"]["url"]):
        errors.append("fan.controller dual needs fan.remote.url (Raspberry Pi) and fan.esp32.url")
    if fan["startup_mode"] not in ("last", "manual", "ai"):
        errors.append("fan.startup_mode must be last, manual or ai")
    if fan["default_mode"] not in ("manual", "ai"):
        errors.append("fan.default_mode must be manual or ai")
    ai = fan["ai"]
    if ai["stale_action"] not in ("off", "on", "hold"):
        errors.append("fan.ai.stale_action must be off, on or hold")
    for name in ("activation_seconds", "recovery_seconds", "min_on_seconds", "min_off_seconds",
                 "stale_seconds", "forecast_confirmations"):
        if not isinstance(ai[name], (int, float)) or ai[name] < 0:
            errors.append(f"fan.ai.{name} must be a number >= 0")
    for pol, lim in (ai["pollutants"] or {}).items():
        try:
            on, off = float(lim["start"]), float(lim["stop"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"fan.ai.pollutants.{pol} needs numbers 'start' and 'stop'")
            continue
        if off >= on:
            errors.append(f"fan.ai.pollutants.{pol}: 'stop' ({off:g}) must be below 'start' ({on:g})")
    if fan["enabled"] and not fan["dry_run"] and fan["controller"] == "remote" and not fan["remote"]["url"]:
        errors.append("fan.remote.url is empty (address of the Raspberry Pi fan agent)")
    fan["operators"] = [str(u) for u in (fan.get("operators") or [])]
    if errors:
        raise ValueError("config.yaml: " + "; ".join(errors))


def validate_setup(cfg):
    """Camera setup and model class settings; raises ValueError."""
    st, m = cfg["setup"], cfg["model"]
    errors = []
    if st["detect_in"] not in ("area", "picture"):
        errors.append("setup.detect_in must be 'area' (YOLO looks only around the detection areas) "
                      "or 'picture' (YOLO looks at the whole picture)")
    for key, top in (("crop_margin", 0.5), ("head_room", 1.0)):
        if not isinstance(st[key], (int, float)) or not 0 <= st[key] <= top:
            errors.append(f"setup.{key} must be a number from 0 to {top}")
    classes = m.get("classes")
    if isinstance(classes, (str, int)):
        classes = [classes]
    if not classes:
        errors.append("model.classes must name the person class of the model, e.g. [person]")
    m["classes"] = [c if isinstance(c, int) else str(c) for c in (classes or [])]
    if errors:
        raise ValueError("config.yaml: " + "; ".join(errors))


def validate_engine(cfg):
    """Detector backend and video decoder settings; raises ValueError."""
    m, p = cfg["model"], cfg["pipeline"]
    errors = []
    m["backend"] = str(m.get("backend") or "ultralytics").lower()
    if m["backend"] not in ("ultralytics", "hailo"):
        errors.append("model.backend must be ultralytics (PC) or hailo (Raspberry Pi AI HAT+ 2)")
    if m["backend"] == "hailo" and not m.get("hef"):
        errors.append("model.backend hailo needs model.hef (the compiled .hef file)")
    p["decoder"] = str(p.get("decoder") or "opencv").lower()
    if p["decoder"] not in ("opencv", "ffmpeg"):
        errors.append("pipeline.decoder must be opencv or ffmpeg")
    p["hwaccel"] = str(p.get("hwaccel") or "none").lower()
    try:
        fps = float(p.get("decode_fps") or 0)
        width = int(p.get("decode_width") or 0)
        if fps < 0 or width < 0:
            raise ValueError
    except (TypeError, ValueError):
        errors.append("pipeline.decode_fps and pipeline.decode_width must be numbers >= 0")
        fps = 0
    if errors:
        raise ValueError("config: " + "; ".join(errors))
    # with ffmpeg delivering fps pictures per second, the two pictures of the optical flow
    # cannot be closer than one picture apart
    if p["decoder"] == "ffmpeg" and fps > 0 and 0 < float(p["flow_interval"]) < 1.0 / fps - 1e-9:
        p["flow_interval"] = round(1.0 / fps, 4)
