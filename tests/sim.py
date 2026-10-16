"""
Synthetic camera for testing the analytics chain without YOLO or a real camera.

Persons are textured rectangles on a textured background. Each analysed picture is
rendered together with the picture one flow interval earlier, exactly like
CameraReader delivers them; the detection boxes are the true boxes plus a little jitter
(like a detector). Everything after YOLO is the real code: optical flow (features.py),
tracking (tracking.py), detection areas (roi.py) and the analytics (analytics.py).
"""
import cv2
import numpy as np

from iaqcam.analytics import Settings, person_measure, sample_summary, window_stats
from iaqcam.features import box_motions
from iaqcam.roi import RoiStore
from iaqcam.tracking import PersonTracker, iou


class Person:
    def __init__(self, x, y, h=300, vx=0.0, vy=0.0, seed=0, visible=lambda t: True, wiggle=0.0):
        self.x, self.y, self.h, self.vx, self.vy = x, y, h, vx, vy   # full-HD pixels, px/s
        self.w = h * 0.4
        self.visible = visible
        self.wiggle = wiggle                # arm-like motion of the upper body, px amplitude
        rng = np.random.default_rng(seed)
        tex = rng.integers(0, 255, (64, 32, 3), dtype=np.uint8)
        self.tex = cv2.resize(cv2.GaussianBlur(tex, (3, 3), 0), (32, 64), interpolation=cv2.INTER_NEAREST)

    @staticmethod
    def _bounce(pos, lo, hi):
        """Walk back and forth between lo and hi instead of leaving the picture."""
        span = hi - lo
        if span <= 0:
            return lo
        k = (pos - lo) % (2 * span)
        return lo + (k if k <= span else 2 * span - k)

    def box(self, t, sx=1.0, sy=1.0):
        x = self._bounce(self.x + self.vx * t, 0.0, 1920.0 - self.w)
        y = self._bounce(self.y + self.vy * t, 0.0, 1080.0 - self.h)
        return (x * sx, y * sy, (x + self.w) * sx, (y + self.h) * sy)


def render(persons, t, size, bg):
    W, H = size
    img = cv2.resize(bg, (W, H), interpolation=cv2.INTER_LINEAR)
    sx, sy = W / 1920.0, H / 1080.0
    for p in persons:
        if not p.visible(t):
            continue
        x1, y1, x2, y2 = (int(round(v)) for v in p.box(t, sx, sy))
        if x2 <= x1 + 2 or y2 <= y1 + 2:
            continue
        tex = cv2.resize(p.tex, (x2 - x1, y2 - y1), interpolation=cv2.INTER_LINEAR)
        if p.wiggle:
            shift = int(round(p.wiggle * sx * np.sin(2 * np.pi * 0.7 * t)))
            top = tex[: (y2 - y1) // 2]
            tex[: (y2 - y1) // 2] = np.roll(top, shift, axis=1)
        xa, ya, xb, yb = max(0, x1), max(0, y1), min(W, x2), min(H, y2)
        if xb > xa and yb > ya:
            img[ya:yb, xa:xb] = tex[ya - y1: yb - y1, xa - x1: xb - x1]
    return img


def gray_small(frame, flow_width=640):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    w = gray.shape[1]
    if w > flow_width:
        gray = cv2.resize(gray, (flow_width, round(gray.shape[0] * flow_width / w)), interpolation=cv2.INTER_AREA)
    return gray, w / gray.shape[1]


def background(seed=1):
    rng = np.random.default_rng(seed)
    bg = rng.integers(60, 200, (270, 480, 3), dtype=np.uint8)
    return cv2.GaussianBlur(bg, (5, 5), 0)


def run_camera(persons, seconds=60, every=5.0, flow_dt=0.12, size=(1920, 1080), roi_polys=None,
               settings=None, jitter=2.0, seed=0, tmpdir=None):
    """Simulate one camera for `seconds`. Returns (window row, per-sample summaries, ids per person)."""
    s = settings or Settings({"analytics": {}})
    bg = background()
    rng = np.random.default_rng(seed)
    roi = None
    if roi_polys is not None:
        import tempfile
        from pathlib import Path
        roi = RoiStore(Path(tmpdir or tempfile.mkdtemp()) / "roi.json")
        roi.set(1, [{"points": pts} for pts in roi_polys])
    tracker = PersonTracker(1)
    samples, people, legacy, ids_of = [], [], [], {i: set() for i in range(len(persons))}
    W, H = size
    t = 0.0
    while t < seconds:
        prev = render(persons, t - flow_dt, size, bg)
        cur = render(persons, t, size, bg)
        g0, scale = gray_small(prev)
        g1, _ = gray_small(cur)
        boxes, who = [], []
        sx, sy = W / 1920.0, H / 1080.0
        for i, p in enumerate(persons):
            if not p.visible(t):
                continue
            b = p.box(t, sx, sy)
            j = rng.normal(0, jitter * sx, 4)
            b = (int(max(0, b[0] + j[0])), int(max(0, b[1] + j[1])), int(min(W - 1, b[2] + j[2])), int(min(H - 1, b[3] + j[3])))
            boxes.append(b)
            who.append(i)
        flows = box_motions(g0, g1, boxes, scale)
        in_roi = roi.classify(1, boxes, W, H) if roi else [True] * len(boxes)
        ids = tracker.update(boxes, cur, t)
        inside = []
        for b, f, ok, (gid, seq, speed), i in zip(boxes, flows, in_roi, ids, who):
            # while two people stand exactly on top of each other nobody can tell them apart:
            # the IDs are checked on the pictures where the person is visible on their own
            if not any(iou(b, o) > 0.5 for o in boxes if o is not b):
                ids_of[i].add(gid)
            a, v, moving = person_measure(f, b, speed, flow_dt, s)
            if ok:
                inside.append({"activity": a, "moving": moving, "gid": gid, "speed": v, "flow": f})
        summary = sample_summary(inside)
        samples.append(summary)
        people += [(p["gid"], p["activity"], p["speed"]) for p in inside]
        vals = [p["flow"] for p in inside if p["flow"] is not None]
        legacy.append(float(np.mean(vals)) if vals else 0.0)
        t += every
    row = window_stats(samples, people, legacy, s.index_ref)
    row["n_samples"] = len(samples)
    return row, samples, ids_of
