"""Can this Raspberry Pi decode all cameras? Run it BEFORE the app (guide, step "Streams").

    .venv/bin/python tools/stream_probe.py
        codec, size and frame rate of the main (1) and sub (2) stream of every camera
    .venv/bin/python tools/stream_probe.py --load 60
        decodes all cameras for 60 s exactly as the app would (pipeline.decoder, stream_index,
        decode_fps, decode_width, hwaccel) and reports pictures per second per camera, CPU
        load and temperature

Verdict: every camera delivers about decode_fps pictures/s, CPU below ~60 % (the detection
loop, optical flow and dashboard need the rest) and the Pi stays below 80 C.
"""
import argparse
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from iaqcam.config import camera_source, load_config, needs_password   # noqa: E402
from iaqcam.videocap import capture_factory, probe                    # noqa: E402


def cpu_times():
    with open("/proc/stat") as fh:
        v = [float(x) for x in fh.readline().split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v), idle


def temperature():
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--load", type=int, default=0, help="seconds of decoding all cameras (0 = only probe)")
    ap.add_argument("--cameras", type=int, nargs="+")
    args = ap.parse_args()
    cfg = load_config(args.config)
    if needs_password(cfg):
        sys.exit("Set NVR_PASSWORD first (export NVR_PASSWORD=...).")
    cams = [c for c in cfg["cameras"] if not args.cameras or c["id"] in args.cameras]

    print(f"{'cam':<5}{'stream':<8}{'codec':<8}{'size':<12}{'fps':>6}   Pi 5 hardware decoder")
    for c in cams:
        for stream in (1, 2):
            info = probe(camera_source(cfg, {**c, "stream_index": stream}))
            if not info:
                print(f"C{c['id']:<4}{stream:<8}{'-':<8}{'no answer':<12}")
                continue
            hw = "yes (H.265)" if info["codec"] in ("hevc", "h265") else "no (software)"
            print(f"C{c['id']:<4}{stream:<8}{info['codec']:<8}{str(info['width']) + 'x' + str(info['height']):<12}"
                  f"{info['fps']:>6.1f}   {hw}")
    if not args.load:
        print("\nNext: --load 60 to measure decoding of all cameras together.")
        return

    p = cfg["pipeline"]
    open_capture = capture_factory(p)
    counts = {c["id"]: 0 for c in cams}
    stop = threading.Event()

    def worker(c):
        cap = open_capture(camera_source(cfg, c))
        if not cap.isOpened():
            print(f"C{c['id']}: cannot open")
            return
        while not stop.is_set():
            if not cap.grab():
                break
            if p["decoder"] == "opencv":
                cap.retrieve()
            counts[c["id"]] += 1
        cap.release()

    threads = [threading.Thread(target=worker, args=(c,), daemon=True) for c in cams]
    for t in threads:
        t.start()
    time.sleep(8)                                       # connect
    base = dict(counts)
    t_all0, t_idle0 = cpu_times()
    temps = []
    t0 = time.time()
    while time.time() - t0 < args.load:
        time.sleep(5)
        temps.append(temperature())
    t_all1, t_idle1 = cpu_times()
    dt = time.time() - t0
    stop.set()
    cpu = 100.0 * (1 - (t_idle1 - t_idle0) / max(1e-9, t_all1 - t_all0))
    print(f"\ndecoder {p['decoder']}, stream_index {cfg['nvr']['stream_index']}, decode_fps {p['decode_fps']}, "
          f"decode_width {p['decode_width']}, hwaccel {p['hwaccel']}")
    rates = {}
    for c in cams:
        rates[c["id"]] = (counts[c["id"]] - base[c["id"]]) / dt
        print(f"  C{c['id']:<3} {rates[c['id']]:6.1f} pictures/s")
    t = [x for x in temps if x is not None]
    print(f"CPU load (all 4 cores): {cpu:.0f} %   temperature: max {max(t):.0f} C" if t else f"CPU load: {cpu:.0f} %")
    want = float(p["decode_fps"]) if p["decoder"] == "ffmpeg" and p["decode_fps"] else 1.0
    slow = [k for k, v in rates.items() if v < 0.8 * want]
    ok = not slow and cpu < 60 and (not t or max(t) < 80)
    print("VERDICT:", "OK - start the app" if ok else
          "TOO HEAVY - " + ("cameras below rate: " + str(slow) + "; " if slow else "") +
          "use sub streams (nvr.stream_index: 2), lower decode_fps or decode_width, check cooling")


if __name__ == "__main__":
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
    main()
