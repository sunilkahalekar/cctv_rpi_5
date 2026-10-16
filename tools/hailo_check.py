"""Check a model before the app uses it: what it outputs, how fast it runs, and whether it finds
the same persons as the .pt model on the PC.

On the Raspberry Pi (AI HAT+ 2):
    .venv/bin/python tools/hailo_check.py --hef models/yolo26s.hef
        input/output layout, decoding, speed (pictures per second) on a test picture
    .venv/bin/python tools/hailo_check.py --hef models/yolo26s.hef --images calib_frames/ --compare reference.json
        persons found by the HEF vs the PC's .pt on the same pictures (recall / precision)

On the PC (conda env cctv_yolo), once, to make reference.json from the same pictures:
    python tools/hailo_check.py --weights yolo26s.pt --imgsz 640 --images calib_frames/ --save reference.json

Accept the HEF when recall and precision are 0.9 or more (persons inside the detection areas
matter most; small, far persons are the first to go after 8-bit quantisation).
"""
import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iaqcam.detector import make_detector            # noqa: E402
from iaqcam.model_classes import person_class_ids    # noqa: E402

PICS = {".jpg", ".jpeg", ".png", ".bmp"}


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def compare(ref, got, thr=0.5):
    """Greedy matching per picture. Returns (true positives, reference boxes, detected boxes)."""
    tp = 0
    used = set()
    for r in ref:
        best, bj = 0.0, None
        for j, g in enumerate(got):
            if j in used:
                continue
            v = iou(r, g)
            if v > best:
                best, bj = v, j
        if bj is not None and best >= thr:
            used.add(bj)
            tp += 1
    return tp, len(ref), len(got)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hef", help="Hailo model (.hef) - on the Pi")
    ap.add_argument("--weights", help="Ultralytics model (.pt) - on the PC, to make the reference")
    ap.add_argument("--imgsz", type=int, default=640)
    ap.add_argument("--labels", default="coco")
    ap.add_argument("--conf", type=float, default=0.25)
    ap.add_argument("--images", help="folder of pictures (e.g. from tools/collect_calib.py)")
    ap.add_argument("--save", help="write the detections to this JSON file")
    ap.add_argument("--compare", help="reference JSON from the PC")
    ap.add_argument("--runs", type=int, default=100, help="pictures for the speed test")
    args = ap.parse_args()
    if not (args.hef or args.weights):
        sys.exit("give --hef (Pi) or --weights (PC)")

    cfg = {"backend": "hailo", "hef": args.hef, "labels": args.labels} if args.hef else \
          {"backend": "ultralytics", "weights": args.weights, "imgsz": args.imgsz, "device": None}
    t0 = time.time()
    det = make_detector(cfg)
    print(f"model      : {det.model_name} ({det.backend}, {det.accelerator}), loaded in {time.time() - t0:.1f} s")
    if hasattr(det, "describe"):
        for k, v in det.describe().items():
            if k == "outputs":
                print("outputs    :")
                for o in v:
                    print("   ", o)
            else:
                print(f"{k:<11}: {v}")
    persons, note = person_class_ids(det.names, ["person"])
    print("classes    :", note)

    pics = sorted(p for p in Path(args.images).rglob("*") if p.suffix.lower() in PICS) if args.images else []
    test = cv2.imread(str(pics[0])) if pics else np.full((720, 1280, 3), 114, np.uint8)

    # speed: single pictures, as the app sends them
    det.predict([test], args.conf, persons)                   # warm-up
    t0 = time.time()
    for _ in range(args.runs):
        det.predict([test], args.conf, persons)
    dt = time.time() - t0
    print(f"speed      : {args.runs / dt:.1f} pictures/s ({1000 * dt / args.runs:.1f} ms each, incl. resize and decoding)")
    print("             the app needs about (cameras x areas) + door fps pictures per second, e.g. 10 x 2 + 5 = 25")

    if not pics:
        print("no --images given: only layout and speed were checked")
        return
    out = {}
    for p in pics:
        img = cv2.imread(str(p))
        if img is None:
            continue
        boxes, confs = det.predict([img], args.conf, persons)[0]
        out[p.name] = [[round(v, 1) for v in b] + [round(c, 3)] for b, c in zip(boxes, confs)]
    n = sum(len(v) for v in out.values())
    print(f"pictures   : {len(out)}, persons found: {n} ({n / max(1, len(out)):.2f} per picture)")
    if args.save:
        Path(args.save).write_text(json.dumps({"model": det.model_name, "conf": args.conf, "detections": out}, indent=1))
        print("saved      :", args.save)
    if args.compare:
        ref = json.loads(Path(args.compare).read_text())["detections"]
        tp = nr = ng = 0
        worst = []
        for name, r in ref.items():
            if name not in out:
                continue
            a, b, c = compare([x[:4] for x in r], [x[:4] for x in out[name]])
            tp, nr, ng = tp + a, nr + b, ng + c
            worst.append((b - a + c - a, name, b, c))
        rec, prec = tp / max(1, nr), tp / max(1, ng)
        print(f"compare    : reference {nr} persons, this model {ng}, matched {tp} (IoU >= 0.5)")
        print(f"             recall {rec:.3f}   precision {prec:.3f}   ->  "
              f"{'ACCEPT' if rec >= 0.9 and prec >= 0.9 else 'CHECK: more/better calibration pictures, or a larger model'}")
        for d, name, b, c in sorted(worst, reverse=True)[:5]:
            if d:
                print(f"             most different: {name}  reference {b}, this model {c}")


if __name__ == "__main__":
    main()
