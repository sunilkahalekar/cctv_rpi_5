"""STEP 2 (Ubuntu / WSL2, inside the compile environment).

Picks calibration pictures from your dataset and writes the compiler settings to match.

    python 2_prepare_calib.py --images /mnt/d/AI/claude/cctv_rpi_5/calib_frames/crops --model yolo26s \
                              --zoo ~/hailo/hailo_model_zoo --onnx yolo26s.onnx

Creates ./calib/ (up to 1024 pictures), custom.alls and custom_nogpu.alls,
and prints the exact compile command.
"""
import argparse
import random
import re
import shutil
import sys
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--images", required=True, help="folder with your training/validation pictures (searched recursively)")
ap.add_argument("--model", required=True, help="Model Zoo name printed by step 1, e.g. yolo26s")
ap.add_argument("--zoo", required=True, help="path of the cloned hailo_model_zoo folder")
ap.add_argument("--onnx", default="best.onnx")
ap.add_argument("--max", type=int, default=1024)
args = ap.parse_args()

alls_src = Path(args.zoo).expanduser() / "hailo_model_zoo" / "cfg" / "alls" / "generic" / f"{args.model}.alls"
if not alls_src.is_file():
    sys.exit(f"STOP: {alls_src} not found. Check --zoo and --model (this Model Zoo version may be too old for YOLO26).")

pics = sorted(p for p in Path(args.images).expanduser().rglob("*") if p.suffix.lower() in {".jpg", ".jpeg", ".png"})
if len(pics) < 64:
    sys.exit(f"STOP: only {len(pics)} pictures found in {args.images}. Use at least a few hundred from the real room.")
random.seed(0)
random.shuffle(pics)
pics = pics[:args.max]
n = len(pics) - len(pics) % 8            # a multiple of the calibration batch size

out = Path("calib")
if out.exists():
    shutil.rmtree(out)
out.mkdir()
for i, p in enumerate(pics[:n]):
    shutil.copy(p, out / f"{i:05d}{p.suffix.lower()}")

text = alls_src.read_text()
text = re.sub(r"calibset_size=\d+", f"calibset_size={n}", text)
Path("custom.alls").write_text(text)
nogpu = "\n".join(line for line in text.splitlines() if "post_quantization_optimization" not in line) + "\n"
Path("custom_nogpu.alls").write_text(nogpu)

print(f"{n} calibration pictures copied to ./calib")
if n < 1024:
    print("NOTE: Hailo recommends 1024 or more pictures from the real scene; fewer usually lowers accuracy.")
print("\nCompile command (GPU machine, best accuracy):")
print(f"  hailomz compile {args.model} --ckpt {args.onnx} --hw-arch hailo10h --calib-path calib --model-script custom.alls")
print("\nIf it stops with an error about GPU or fine-tune (for example under WSL2), use:")
print(f"  hailomz compile {args.model} --ckpt {args.onnx} --hw-arch hailo10h --calib-path calib --model-script custom_nogpu.alls")
print(f"\nResult: {args.model}.hef in this folder.")
