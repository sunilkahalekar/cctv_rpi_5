# Making the .hef files for the AI HAT+ 2 (Hailo-10H)

The Pi runs only `.hef` files. Compiling needs an x86 Linux computer: Ubuntu 22.04 in WSL2 on the lab PC is enough.
You need two files on the Pi: `models/yolo26s.hef` (detection loop) and, optionally, `models/yolo26n.hef` (door counter; without it the door shares `yolo26s.hef`).

## Route 0 — ready-made file (fastest, for a first run)

If Hailo's Model Zoo for your HailoRT version lists a precompiled `yolo26s` for **HAILO10H** (Model Zoo GitHub, `docs/public_models/HAILO10H/HAILO10H_object_detection.rst`), download it and copy it to `models/yolo26s.hef`. It is calibrated on COCO, not on our lab, so check it with `tools/hailo_check.py` and recompile with Route A later.

## Route A — Model Zoo compiler (proven in edge_ai_aqi_event_aware)

1. **Versions must match.** On the Pi: `hailortcli --version` (5.3 or newer for YOLO26). Use the same Dataflow Compiler (`hailo_dataflow_compiler-5.3.x-...whl`, Hailo Developer Zone) and `git clone -b v5.3.0 https://github.com/hailo-ai/hailo_model_zoo.git`.
2. **ONNX (Windows, conda env `cctv_yolo`, in `D:\AI\claude\cctv_rpi_5`):**
   `python tools/hailo_compile/1_export_onnx.py --weights D:\AI\claude\CCTV_Yolo\yolo26s.pt` → `yolo26s.onnx`, `labels.txt` next to the .pt, and `PASS: all 6 output layers found.`
3. **Calibration pictures of our lab (Windows or Pi):**
   `python tools/collect_calib.py --config config.yaml --rounds 30 --every 120` (repeat at different times of day until `calib_frames/crops` holds 1,024+ pictures).
4. **WSL2 Ubuntu 22.04** (once):
   ```bash
   sudo apt update && sudo apt install -y python3-dev python3-pip python3-venv python3-tk graphviz graphviz-dev build-essential git unzip
   mkdir -p ~/hailo && cd ~/hailo
   cp /mnt/c/Users/$USER/Downloads/hailo_dataflow_compiler-*.whl .
   python3 -m venv dfc-env && source dfc-env/bin/activate
   pip install --upgrade pip && pip install hailo_dataflow_compiler-*.whl
   git clone -b v5.3.0 https://github.com/hailo-ai/hailo_model_zoo.git
   cd hailo_model_zoo && pip install -e . && cd ..
   ```
5. **Compile:**
   ```bash
   cp /mnt/d/AI/claude/CCTV_Yolo/yolo26s.onnx /mnt/d/AI/claude/cctv_rpi_5/tools/hailo_compile/2_prepare_calib.py .
   python 2_prepare_calib.py --images /mnt/d/AI/claude/cctv_rpi_5/calib_frames/crops --model yolo26s --zoo ~/hailo/hailo_model_zoo --onnx yolo26s.onnx
   hailomz compile yolo26s --ckpt yolo26s.onnx --hw-arch hailo10h --calib-path calib --model-script custom.alls
   ```
   No GPU in WSL2 → if it stops on a GPU / fine-tune error, run the second command the script printed (`custom_nogpu.alls`).
   Repeat with `yolo26n.pt` for the door model (optional).
6. **Copy to the Pi:** `scp yolo26s.hef pi@<pi>:~/cctv_rpi_5/models/yolo26s.hef`

## Route B — Ultralytics export

`python tools/hailo_compile/export_ultralytics_hailo.py --weights yolo26s.pt --calib <calib_frames/crops>` in an Ubuntu environment with Ultralytics and the DFC 5.x wheel. One command, less control.

## Check on the Pi before using it

```bash
cctv_rpi/bin/python tools/hailo_check.py --hef models/yolo26s.hef                      # layout + speed
# PC:  python tools/hailo_check.py --weights yolo26s.pt --imgsz 640 --images calib_frames/full --save reference.json
cctv_rpi/bin/python tools/hailo_check.py --hef models/yolo26s.hef --images calib_frames/full --compare reference.json
```
Accept at recall and precision ≥ 0.9. Custom-trained model (e.g. from `yolo_custom_training`): same steps with its `best.pt`, then `model: labels: models/labels.txt` in `config.yaml`.
