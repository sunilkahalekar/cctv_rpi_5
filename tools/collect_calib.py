"""Pictures of OUR lab for compiling the Hailo model (calibration) and for checking it.

The compiler runs these pictures through the network to choose its 8-bit ranges, so they must
look like what the app shows the model: the parts around each camera's detection areas, at
different times of day, with and without people. Run it on the PC (where roi.json is) or on
the Pi, a few times a day over several days, until there are 1,024 or more.

    python tools/collect_calib.py --config config.yaml --rounds 20 --every 60
        every 60 s, one picture per camera: full picture -> calib_frames/full/,
        the parts YOLO looks at -> calib_frames/crops/
    set NVR_PASSWORD first (Windows: set NVR_PASSWORD=...; Pi: export NVR_PASSWORD=...).
"""
import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
import cv2  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iaqcam.config import camera_source, load_config, needs_password   # noqa: E402
from iaqcam.roi import RoiStore                                         # noqa: E402


def grab(source, tries=3):
    for _ in range(tries):
        cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
        ok, frame = cap.read() if cap.isOpened() else (False, None)
        for _ in range(5):                     # a few frames later: past the first, often grey, frame
            ok2, f2 = cap.read()
            if ok2:
                ok, frame = ok2, f2
        cap.release()
        if ok and frame is not None:
            return frame
        time.sleep(1)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--out", default="calib_frames")
    ap.add_argument("--rounds", type=int, default=10, help="pictures per camera")
    ap.add_argument("--every", type=float, default=60, help="seconds between rounds")
    ap.add_argument("--cameras", type=int, nargs="+")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if needs_password(cfg):
        sys.exit("Set NVR_PASSWORD first.")
    cams = [c for c in cfg["cameras"] if not args.cameras or c["id"] in args.cameras]
    st = cfg["setup"]
    roi = RoiStore(cfg["roi_file"], cfg.get("roi"), st["require_setup"], st["crop_margin"], st["head_room"])
    full, crops = Path(args.out) / "full", Path(args.out) / "crops"
    full.mkdir(parents=True, exist_ok=True)
    crops.mkdir(parents=True, exist_ok=True)
    n_full = n_crop = 0
    for r in range(args.rounds):
        t0 = time.time()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for c in cams:
            if roi.mode(c["id"]) == "skip":
                continue
            frame = grab(camera_source(cfg, c))
            if frame is None:
                print(f"  camera {c['id']}: no picture")
                continue
            h, w = frame.shape[:2]
            cv2.imwrite(str(full / f"cam{c['id']}_{stamp}.jpg"), frame)
            n_full += 1
            rects = roi.crops(c["id"], w, h, bool(st["crop_per_area"])) if st["detect_in"] == "area" else []
            for k, (x1, y1, x2, y2) in enumerate(rects or [(0, 0, w, h)]):
                cv2.imwrite(str(crops / f"cam{c['id']}_{stamp}_a{k}.jpg"), frame[y1:y2, x1:x2])
                n_crop += 1
        print(f"round {r + 1}/{args.rounds}: {n_full} full pictures, {n_crop} parts so far")
        if r + 1 < args.rounds:
            time.sleep(max(0.0, args.every - (time.time() - t0)))
    total = len(list(crops.glob("*.jpg")))
    print(f"\n{total} pictures in {crops} (target: 1,024 or more, from different times of day)")


if __name__ == "__main__":
    main()
