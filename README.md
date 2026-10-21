# cctv_rpi_5 — lab CCTV vision, air quality and exhaust fan on Raspberry Pi 5 + AI HAT+ 2

The complete MS 101 system on one Raspberry Pi 5 with the AI HAT+ 2 (Hailo-10H):

- 10 NVR cameras (VIVOTEK ND9425P, read-only RTSP sub streams), decoded by ffmpeg — H.265 on the Pi 5's hardware decoder
- YOLO26 person detection on the AI HAT+ 2 (`models/yolo26s.hef`), only inside each camera's detection areas
- person IDs and tracking, optical-flow motion per person, door in/out counting (`models/yolo26n.hef`)
- per-second and per-minute rows in SQLite (`data/` on the USB drive)
- ThingSpeak ILCS #2 sensors, 10-minute forecast, physics / grey-box models, pollution-in-transit early warning
- exhaust fan in Manual or AI mode through the **ESP32 only** (`esp32_fan_agent/`)
- dashboard with sign-in on port 8001, reachable from the lab network

It started from the PC app in `D:\AI\claude\CCTV_Yolo` (lab-vision-iaq v8.4), which stays separate and unchanged.

## Start here

**docs/RPI5_SETUP.md** — hardware, OS, setup scripts, model, stream check, first run, ESP32 fan, service, one week side by side with the PC.

```bash
cd ~/cctv_rpi_5
bash deploy/rpi5/00_capacity_check.sh 120    # first: room next to what already runs on this Pi
bash deploy/rpi5/01_system.sh && bash deploy/rpi5/02_hailo.sh && sudo reboot
bash deploy/rpi5/03_app.sh                   # creates the virtual environment cctv_rpi/
source cctv_rpi/bin/activate                 # (cctv_rpi) prompt; or call cctv_rpi/bin/python directly
cctv_rpi/bin/python tools/hailo_check.py --hef models/yolo26s.hef
cctv_rpi/bin/python tools/stream_probe.py --load 60
cctv_rpi/bin/python run.py                      # first run by hand (fan in dry run)
bash deploy/rpi5/04_service.sh               # then as a service
```

## Folder

| Path | What |
|---|---|
| `run.py`, `config.yaml` | the program and its settings |
| `iaqcam/` | the app: `detector.py` (Hailo / Ultralytics), `videocap.py` (ffmpeg), `bytetrack.py` (door), pipeline, tracking, door, fan, forecast, physics, transit, web |
| `models/` | `.hef` files (made with `tools/hailo_compile/`) |
| `deploy/rpi5/` | capacity check 00, setup scripts 01–05, systemd service, nightly backup |
| `tools/` | `hailo_check.py`, `stream_probe.py`, `collect_calib.py`, `archive_seconds.py`, `hailo_compile/` |
| `esp32_fan_agent/` | the ESP32 sketch (GPIO 26 → SSR) |
| `machine_agent/`, `research/` | machine reports; offline research evaluation and figures |
| `roi.json`, `door_line.json` | detection areas and door line (from the PC, 2026-10-16) |
| `docs/` | `RPI5_SETUP.md` (this system), `APP_REFERENCE.md` (features, pages, calibration) |
| `tests/` | `python -m pytest -q tests` (80 tests) |

Secrets are never in the folder: NVR password and ESP32 token live in `/etc/cctv-rpi5.env` on the Pi; `users.json` and `esp32_agent_token.txt` are copied from the PC.
