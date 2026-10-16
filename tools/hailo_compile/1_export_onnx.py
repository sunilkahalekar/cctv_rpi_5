"""STEP 1 (Windows PC, conda env cctv_yolo).

Checks a YOLO26 detection model (the COCO yolo26s.pt the app uses, or a custom-trained
best.pt), exports it to ONNX in the form Hailo's compiler needs (NMS-free head cut at the
six output convolutions), verifies the export, and writes labels.txt.

    python tools/hailo_compile/1_export_onnx.py --weights yolo26s.pt

Outputs next to the weights:  yolo26s.onnx  and  labels.txt
(Taken from edge_ai_aqi_event_aware/pc_compile, where it was written for the same Hailo-10H.)
"""
import argparse
import sys
from pathlib import Path

from ultralytics import YOLO

ap = argparse.ArgumentParser()
ap.add_argument("--weights", required=True)
ap.add_argument("--imgsz", type=int, default=640, help="keep 640: Hailo's YOLO26 config is built for 640x640")
args = ap.parse_args()

model = YOLO(args.weights)
head = model.model.model[-1]
head_name = type(head).__name__
head_index = len(model.model.model) - 1
scale = (model.model.yaml or {}).get("scale") or "?"
train = (model.ckpt or {}).get("train_args", {}) if hasattr(model, "ckpt") else {}
names = [model.names[i] for i in range(len(model.names))]

print("task        :", model.task)
print("head        :", head_name, f"(layer {head_index})")
print("size letter :", scale)
print("trained from:", train.get("model"), "| training imgsz:", train.get("imgsz"))
print("classes     :", names)

if head_name == "Segment26":
    kind, branches, zoo = "segmentation", (2, 3, 4), f"yolo26{scale}_seg"
elif head_name == "Detect" and getattr(head, "reg_max", 16) == 1:
    kind, branches, zoo = "detection", (2, 3), f"yolo26{scale}"
else:
    sys.exit(f"\nSTOP: head '{head_name}' is not a YOLO26 detection/segmentation head. "
             "This guide's Hailo route is for YOLO26 only - send this output to get the right steps.")
if scale not in "nsml" or (kind == "detection" and scale == "l"):
    print(f"\nWARNING: Hailo's Model Zoo has no ready config named {zoo}. Send this output before compiling.")

import importlib  # noqa: E402

want = [f"/model.{head_index}/one2one_cv{b}.{i}/one2one_cv{b}.{i}.2/Conv" for b in branches for i in range(3)]
if kind == "segmentation":
    want.append(f"/model.{head_index}/proto/cv3/act/Mul")

print(f"\nExporting {kind} model to ONNX ({args.imgsz}x{args.imgsz}, opset 11, NMS-free head) ...")
onnx_path, missing = None, want
# Ultralytics versions name the "NMS-free (one-to-one) head" option differently, so try each form
for extra in ({"nms": False}, {"end2end": True}, {}):
    try:
        onnx_path = YOLO(args.weights).export(format="onnx", imgsz=args.imgsz, opset=11, **extra)
    except Exception as e:                       # option not known in this version
        print(f"   (export with {extra} not accepted: {e})")
        continue
    onnx = importlib.import_module("onnx")      # installed by the export step if it was missing
    have = {n.name for n in onnx.load(onnx_path).graph.node}
    missing = [w for w in want if w not in have]
    if not missing:
        break
    print(f"   (export with {extra} does not contain the NMS-free layers, trying another form)")

labels = Path(args.weights).with_name("labels.txt")
labels.write_text("\n".join(names) + "\n", encoding="utf-8")

print("\nONNX file  :", onnx_path)
print("labels file:", labels, f"({len(names)} classes)")
if missing:
    print("\nFAIL: the ONNX is missing the layers Hailo's config cuts at:")
    for m in missing:
        print("   ", m)
    print("Update Ultralytics (pip install -U ultralytics) and run this script again.")
    sys.exit(1)
if head_index != 23:
    print(f"\nWARNING: the head is layer {head_index}, Hailo's config expects layer 23. Send this output before compiling.")
print(f"\nPASS: all {len(want)} output layers found.")
print(f"Model Zoo name to use in the compile step:  {zoo}")
