# cctv_rpi_5 — the complete lab CCTV system on a Raspberry Pi 5 + AI HAT+ 2

Version 1 (2026-10-16). Folder `D:\AI\claude\cctv_rpi_5`, dedicated to the CCTV system on the Raspberry Pi 5 with the AI HAT+ 2. It started from the PC app (`D:\AI\claude\CCTV_Yolo`, lab-vision-iaq v8.4), which stays separate and unchanged. The exhaust fan is switched by the **ESP32 only**.

```
 NVR VIVOTEK ND9425P (10 cameras, read-only RTSP)              ThingSpeak ILCS #2 (internet)
        │ sub streams, H.264/H.265                                     │ every 20 s
        ▼                                                              ▼
 ┌──────────────────────────── Raspberry Pi 5 (Trixie 64-bit) ───────────────────────────┐
 │ 10 × ffmpeg decoder processes  (H.265 → Pi 5 hardware decoder, H.264 → CPU)           │
 │   └─ 10 pictures/s per camera, scaled to 960 px                                       │
 │ camera threads ── 1 picture/s + flow partner 0.1 s earlier ──┐                        │
 │ door camera ── 5 pictures/s while someone moves ─────────────┤                        │
 │                                                              ▼                        │
 │            AI HAT+ 2 (Hailo-10H, PCIe)  yolo26s.hef 640×640 / yolo26n.hef (door)      │
 │                 detection areas only (crops), persons only                            │
 │                                                              ▼                        │
 │ tracking (IDs, held persons) · optical-flow motion · door in/out (ByteTrackLite)      │
 │ per-second + per-minute rows → SQLite on the USB drive (data/ → /mnt/usbdata/cctv)    │
 │ forecast (10 min) · physics / grey-box · transit early warning · events               │
 │ fan policy (Manual / AI) ─────────── HTTP + token, heartbeat 15 s ──────┐             │
 │ dashboard :8001 (sign-in) ── lab network                                │             │
 └─────────────────────────────────────────────────────────────────────────┼─────────────┘
                                                                           ▼
                                         ESP32 fan agent (GPIO 26 → SSR → exhaust fan)
                                         watchdog 120 s: no contact → fan OFF
```

**What differs from the PC app** (`CCTV_Yolo`, left as it is):

| Part | PC app (`CCTV_Yolo`) | This system (`cctv_rpi_5`) |
|---|---|---|
| Detector | Ultralytics `yolo26s.pt` at 960 px on CPU/GPU | `model.backend: hailo`, `models/yolo26s.hef` 640×640 on the Hailo-10H via HailoRT, no torch (`iaqcam/detector.py`) |
| Door counter | `yolo26n.pt` + Ultralytics ByteTrack | `models/yolo26n.hef` (or the shared `yolo26s.hef`) + `ByteTrackLite` (`iaqcam/bytetrack.py`) |
| Video | OpenCV, main streams | `pipeline.decoder: ffmpeg`, one process per camera, sub streams, H.265 in hardware, 10 pictures/s, 960 px (`iaqcam/videocap.py`) |
| Fan | Raspberry Pi 4 + ESP32 (`dual`) | `fan.controller: esp32` only (`Esp32FanController`) |
| Data | `iaq_cctv.db` next to the app | `data/iaq_cctv.db`, `data` → USB drive |
| Start | `python run.py` | systemd service `cctv-rpi5` |

Everything after the detector — detection areas, tracking, motion, minute rows, forecast, physics, transit warnings, experiments, dashboard pages — is the same code.

---

## 1. Hardware

- Raspberry Pi 5, **8 GB** recommended (4 GB works), official 27 W USB-C supply, **Active Cooler** (the HAT sits over it).
- AI HAT+ 2 with the 16 mm stacking header, spacers and ribbon (contacts toward the Pi's USB ports on the Pi side).
- 64 GB A2 microSD for the system; USB 3 SSD or drive for the data (the HAT occupies the PCIe port, so no M.2 SSD).
- Wired Ethernet on the lab network (10.185.x), a fixed address or DHCP reservation, e.g. 10.185.101.210.
- The ESP32 fan agent already working (`esp32_fan_agent/esp32_fan_agent.ino`, GPIO 26 → SSR, 10 kΩ pull-down).

## 2. Operating system

1. Raspberry Pi Imager → **Raspberry Pi OS (64-bit) Trixie** (Lite is enough). In the Imager settings: hostname `cctvpi`, user `pi`, SSH with your key, time zone Asia/Kolkata.
2. First boot, then:
   ```bash
   sudo apt update && sudo apt full-upgrade -y && sudo rpi-eeprom-update -a && sudo reboot
   ```
3. USB drive at `/mnt/usbdata` (as on the earlier Pi 5 setup; skip if already there). `lsblk -f` shows it, e.g. `sda1` with a UUID:
   ```bash
   sudo mkdir -p /mnt/usbdata
   echo "UUID=<the-uuid> /mnt/usbdata ext4 defaults,noatime,nofail 0 2" | sudo tee -a /etc/fstab
   sudo mount -a && df -h /mnt/usbdata
   ```
   (A new drive must first be formatted: `sudo mkfs.ext4 -L usbdata /dev/sda1` — this erases it.)

## 3. The app on the Pi

From the PC, PowerShell (copies the whole folder; it holds no database, model weights or secrets):
```powershell
scp -r D:\AI\claude\cctv_rpi_5 pi@cctvpi.local:~/
```
On the Pi the app is then in `~/cctv_rpi_5`; all commands below are run there (`cd ~/cctv_rpi_5`).

**Settings from the PC app.** `roi.json` and `door_line.json` (detection areas, door line) are already in the folder, copied from `CCTV_Yolo` on 2026-10-16. Copy the dashboard users and the ESP32 token (they are secrets, so not in the folder). PowerShell in `D:\AI\claude\CCTV_Yolo`:
```powershell
scp users.json esp32_agent_token.txt pi@cctvpi.local:~/cctv_rpi_5/
```
After areas are changed on the PC, copy `roi.json` / `door_line.json` again (or edit them on the Pi's own Setup page). Both files hold fractions of the picture, so they stay right with the smaller sub-stream pictures (same 16:9 shape).

## 4. Setup scripts (on the Pi, in `~/cctv_rpi_5`)

```bash
bash deploy/rpi5/01_system.sh      # packages, ffmpeg, watchdog, log cap, clock, data/ -> USB drive
bash deploy/rpi5/02_hailo.sh       # hailo-h10-all (driver, firmware, HailoRT)
sudo reboot
hailortcli fw-control identify     # must say HAILO10H; write down hailortcli --version (5.3+ for YOLO26)
bash deploy/rpi5/03_app.sh         # virtual environment (no torch), checks, tests
```

## 5. Model (.hef)

Follow `tools/hailo_compile/README.md`. Short version:
- **First run:** a ready-made `yolo26s` HEF for HAILO10H from Hailo's Model Zoo, if your version lists one → `models/yolo26s.hef`.
- **For the lab (recommended):** collect 1,024+ pictures of our detection areas (`python tools/collect_calib.py`), export ONNX on Windows (`tools/hailo_compile/1_export_onnx.py`), compile in WSL2 Ubuntu with the Dataflow Compiler that matches the Pi's HailoRT (`2_prepare_calib.py` + `hailomz compile yolo26s ... --hw-arch hailo10h`). Same for `yolo26n` (door), optional.
- **Check on the Pi:**
  ```bash
  .venv/bin/python tools/hailo_check.py --hef models/yolo26s.hef
  ```
  It prints the output layout (raw / nms / table — the app decodes all three) and the speed. Then compare with the PC on the same pictures (recall and precision ≥ 0.9).

## 6. Streams: can the Pi decode all 10 cameras?

```bash
export NVR_PASSWORD='...'
.venv/bin/python tools/stream_probe.py            # codec and size of main/sub stream
.venv/bin/python tools/stream_probe.py --load 60  # all cameras together
```
Wanted: every camera ≈ 10 pictures/s, CPU < 60 %, temperature < 80 °C. If the sub stream is too small for a far camera (persons missed), give that camera its main stream in `config.yaml`: `- {id: 6, name: "Camera 6", location: "...", stream_index: 1}` — affordable when it is H.265 (hardware decoder), costly when H.264.

## 7. First run by hand (fan in dry run)

```bash
export NVR_PASSWORD='...'
.venv/bin/python run.py
```
Expected lines: `Video: ffmpeg, 10 pictures/s per camera ...`, `Model yolo26s.hef at 640px on Hailo-10H, 10 camera(s) ...`, `Door counter on Camera 2 (yolo26n.hef ...)`, `Exhaust fan: DRY RUN`, then `[minute]` lines every minute. Open `http://<pi-address>:8001` from a lab PC and sign in (the same users as on the PC, from `users.json`).
Check: Overview counts, every camera picture with boxes, Setup areas as on the PC, Door page, Air page (ThingSpeak), Events. Ctrl+C stops.

## 8. Exhaust fan through the ESP32 only

1. Token: the TOKEN of `esp32_fan_agent/secrets.h` is in `esp32_agent_token.txt` (copied in step 3) or given to `04_service.sh`.
2. The address in `config.yaml → fan → esp32 → url` is the one the PC uses today (`http://10.185.103.170:8095`, router port-forward to the ESP32 at 192.168.0.200). Check from the Pi: `curl -s -H "Authorization: Bearer $(cat esp32_agent_token.txt)" http://10.185.103.170:8095/status`.
3. **Only one system may command the ESP32.** Before switching on the Pi: in the PC app set `fan: enabled: false` in `CCTV_Yolo\config.yaml` (or stop the PC app).
4. `config.yaml`: `dry_run: false`, restart. Fan page: Manual → ON → the SSR LED and the fan come on; OFF. Then unplug the Pi's network cable for > 2 minutes: the ESP32 watchdog must switch the fan OFF.
5. AI mode only after the thresholds are calibrated (README "Exhaust fan").

## 9. Service, backup, health

```bash
bash deploy/rpi5/04_service.sh     # asks NVR user/password and ESP32 token once -> /etc/cctv-rpi5.env (root only)
journalctl -u cctv-rpi5 -f         # live log
bash deploy/rpi5/05_health.sh      # HAT, temperature, throttling, newest rows per table
```
The service starts at boot, restarts after a crash (10 s), and stops its ffmpeg decoders with it. A copy of the database is made every night at 02:40 to `/mnt/usbdata/backups/cctv` (30 days).
Updates: copy the changed files from `D:\AI\claude\cctv_rpi_5` (scp) — or keep the folder in its own GitHub repository and `git pull` — then `bash deploy/rpi5/03_app.sh && sudo systemctl restart cctv-rpi5`.

## 10. One week side by side, then switch

Run the PC app (`CCTV_Yolo`, fan disabled) and the Pi together for a week; each writes its own database. Compare per camera and minute: `n_person`, `motion_total`, door in/out. Differences of a person now and then are normal (different picture size and model precision); a camera that is clearly lower on the Pi needs its main stream or a recompiled HEF with more calibration pictures. Then the Pi takes over the fan and the PC can be used for research only (`research/greybox_eval.py`, figures — copy the Pi's database to the PC for that).

## 11. Load budget (estimate, to be confirmed with steps 5–6)

| Work | Per second | Where |
|---|---|---|
| Video decoding | 10 cameras × 25 fps sub stream (decoded), 100 pictures handed over | ffmpeg processes, CPU / H.265 decoder |
| Detection | ~10–20 area pictures (1/s per camera, 1–2 areas) + up to 5 door pictures | Hailo-10H: tens of ms each, large headroom |
| Optical flow | 10 × one 480 px pair | CPU (numpy/OpenCV) |
| Tracking, minute rows, forecast, dashboard JPEGs (640 px) | small | CPU |

## 12. Problems and fixes

| Symptom | Cause | Fix |
|---|---|---|
| `HEF file not found` at start | model not copied | step 5; or `model: backend: ultralytics` + `pip install ultralytics` for a CPU-only test (slow) |
| `cannot tell boxes from class scores` / `not in the NMS-by-class layout` | a HEF with another output layout | `tools/hailo_check.py` output → send it; recompile with Route A |
| `hailo_platform` not importable | driver missing or wrong package | `02_hailo.sh`, reboot; never `hailo-all` |
| Cameras `cannot connect` | password, stream index, network | `export NVR_PASSWORD`; `run.py --test 8`; stream_probe |
| CPU 100 %, pictures/s low | main streams in software | `stream_index: 2`, `decode_fps: 8`, `decode_width: 800` |
| `throttled=0x...` not 0 | supply or heat | 27 W supply, Active Cooler, case airflow |
| Fan page: ESP32 not reachable | URL/token/port forward | curl test in step 8; token = secrets.h TOKEN |
| Counts lower than on the PC | small sub-stream persons, quantisation | main stream for that camera; recompile with lab calibration pictures |

## 13. Tested and not yet tested

Tested without the Pi (cloud test machine, 2026-10-16): all 78 automatic tests (65 from the PC app + 13 for the Pi) pass. They cover YOLO26 raw / NMS / table decoding, letterbox, a stand-in for HailoRT's Python API driving the real detection loop, ByteTrackLite, the ffmpeg reader and the ESP32-only controller against the mock agent. A full run of `run.py` also worked, with two test streams through ffmpeg and a simulated Hailo chip: per-minute rows, door IN counts, dashboard, and ffmpeg processes closed on exit.

**Not yet tested on real hardware:** HailoRT 5.x on the AI HAT+ 2 (the API calls follow the `engine_hef.py` of the earlier Pi 5 project, also not confirmed there), the output layout of the HEF you compile, H.265 hardware decoding of the NVR streams, the CPU load with 10 real cameras, and the ESP32 switched by the Pi.
