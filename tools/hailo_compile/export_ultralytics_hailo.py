"""ROUTE B (Ubuntu / WSL2 on an x86 PC): Ultralytics compiles the .hef in one command.

Needs the Hailo Dataflow Compiler 5.x wheel (Hailo Developer Zone, free login) installed in the
same Python environment as Ultralytics. Route A (Model Zoo, README.md here) is the one proven
with this lab's earlier Pi 5 project; use B when A's Model Zoo has no config for the model.

    python export_ultralytics_hailo.py --weights yolo26s.pt --calib /mnt/d/AI/claude/cctv_rpi_5/calib_frames/crops

Result: a folder yolo26s_hailo_model/ with yolo26s.hef and metadata.yaml. Copy the .hef to the
Pi's models/ folder; tools/hailo_check.py tells which output layout it has (the app decodes
raw, nms and table layouts).
"""
import argparse
import shutil
import tempfile
from pathlib import Path

from ultralytics import YOLO

ap = argparse.ArgumentParser()
ap.add_argument("--weights", default="yolo26s.pt")
ap.add_argument("--imgsz", type=int, default=640)
ap.add_argument("--calib", help="folder of calibration pictures (tools/collect_calib.py -> calib_frames/crops)")
ap.add_argument("--chip", default="hailo10h", help="hailo10h = Raspberry Pi AI HAT+ 2")
args = ap.parse_args()

extra = {}
if args.calib:
    # Ultralytics takes calibration pictures as a dataset yaml: make a tiny one pointing at them
    pics = [p for p in Path(args.calib).rglob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png"}]
    print(f"{len(pics)} calibration pictures in {args.calib}" + ("  (1,024 or more recommended)" if len(pics) < 1024 else ""))
    tmp = Path(tempfile.mkdtemp(prefix="calib_"))
    (tmp / "images" / "val").mkdir(parents=True)
    for i, p in enumerate(pics):
        shutil.copy(p, tmp / "images" / "val" / f"{i:05d}{p.suffix.lower()}")
    names = YOLO(args.weights).names
    lines = [f"path: {tmp}", "train: images/val", "val: images/val", "names:"]
    lines += [f"  {i}: {n}" for i, n in sorted(names.items())]
    (tmp / "calib.yaml").write_text("\n".join(lines) + "\n")
    extra["data"] = str(tmp / "calib.yaml")

out = YOLO(args.weights).export(format="hailo", name=args.chip, imgsz=args.imgsz, **extra)
print("\nexported:", out)
print("copy the .hef to the Pi:  scp <folder>/*.hef pi@<pi-address>:~/cctv_rpi_5/models/yolo26s.hef")
