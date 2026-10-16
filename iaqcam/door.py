"""
Door counter: counts people walking in and out through one door.

A separate thread follows the door camera at a few frames per second with a small
YOLO model and the ByteTrack tracker. Each tracked person is represented by the
point where they stand (bottom centre of the box). When that point moves from one
side of the counting line to the other, and the path really passes through the
line segment, one "in" or "out" event is stored.

To keep the CPU free, frames are only analysed while something is moving in the
picture or a person is in view.
"""
import json
import math
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import cv2

from .model_classes import person_class_ids

MARGIN_FRACTION = 0.012     # a point this close to the line (fraction of the diagonal) is undecided
HOLD_SECONDS = 2.5          # keep analysing this long after the last motion / person
TRACK_FORGET_SECONDS = 4.0


# ----------------------------------------------------------------- geometry
def _cross(ax, ay, bx, by):
    return ax * by - ay * bx


def signed_distance(p, a, b):
    """Distance of p from the line through a-b; the sign tells the side."""
    length = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
    return _cross(b[0] - a[0], b[1] - a[1], p[0] - a[0], p[1] - a[1]) / length


def segments_intersect(p1, p2, a, b):
    """True when the path p1->p2 passes through the segment a-b."""
    d1 = _cross(b[0] - a[0], b[1] - a[1], p1[0] - a[0], p1[1] - a[1])
    d2 = _cross(b[0] - a[0], b[1] - a[1], p2[0] - a[0], p2[1] - a[1])
    d3 = _cross(p2[0] - p1[0], p2[1] - p1[1], a[0] - p1[0], a[1] - p1[1])
    d4 = _cross(p2[0] - p1[0], p2[1] - p1[1], b[0] - p1[0], b[1] - p1[1])
    return (d1 * d2 < 0) and (d3 * d4 < 0)


class DoorLine:
    """The counting line in picture fractions (0..1), editable from the dashboard."""

    def __init__(self, path, line, inside_point):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.line = [[float(v) for v in p] for p in line]
        self.inside_point = [float(v) for v in inside_point]
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                self._apply(saved["line"], saved["inside_point"])
            except Exception as exc:
                print(f"[door] could not read {self.path.name} ({exc}); using config.yaml")

    @staticmethod
    def _valid(line, inside_point):
        pts = [*line, inside_point]
        if len(line) != 2 or any(len(p) != 2 for p in pts):
            return False
        if not all(isinstance(v, (int, float)) and -0.05 <= v <= 1.05 for p in pts for v in p):
            return False
        return math.hypot(line[1][0] - line[0][0], line[1][1] - line[0][1]) > 0.03 \
            and abs(signed_distance(inside_point, line[0], line[1])) > 1e-4

    def _apply(self, line, inside_point):
        if not self._valid(line, inside_point):
            raise ValueError("line needs two different points and an inside point off the line")
        with self._lock:
            self.line = [[float(v) for v in p] for p in line]
            self.inside_point = [float(v) for v in inside_point]

    def set(self, line, inside_point):
        self._apply(line, inside_point)
        self.path.write_text(json.dumps({"line": self.line, "inside_point": self.inside_point},
                                        indent=2), encoding="utf-8")

    def get(self):
        with self._lock:
            return [list(p) for p in self.line], list(self.inside_point)

    def pixels(self, width, height):
        """(a, b, inside sign) in pixels of a width x height frame."""
        line, inside = self.get()
        a = (line[0][0] * width, line[0][1] * height)
        b = (line[1][0] * width, line[1][1] * height)
        sign = 1 if signed_distance((inside[0] * width, inside[1] * height), a, b) > 0 else -1
        return a, b, sign


class CrossingCounter:
    """Turns tracked foot points into in / out events. Pure logic, no video."""

    def __init__(self):
        self.tracks = {}

    def update(self, track_id, point, now, a, b, inside_sign, margin):
        """Returns 'in', 'out' or None for this observation of one track."""
        st = self.tracks.setdefault(track_id, {"side": 0, "pt": point, "seen": now})
        st["seen"] = now
        d = signed_distance(point, a, b)
        if abs(d) < margin:                      # too close to call
            return None
        side = 1 if d > 0 else -1
        event = None
        if st["side"] and side != st["side"]:
            # extend the segment a little so that grazing its ends still counts
            ex, ey = (b[0] - a[0]) * 0.06, (b[1] - a[1]) * 0.06
            if segments_intersect(st["pt"], point, (a[0] - ex, a[1] - ey), (b[0] + ex, b[1] + ey)):
                event = "in" if side == inside_sign else "out"
        st["side"], st["pt"] = side, point
        return event

    def forget_old(self, now):
        for tid in [t for t, st in self.tracks.items() if now - st["seen"] > TRACK_FORGET_SECONDS]:
            del self.tracks[tid]

    def clear(self):
        self.tracks.clear()


class DoorCounter(threading.Thread):
    def __init__(self, cfg, reader, db, state, line):
        super().__init__(daemon=True, name="door-counter")
        d = cfg["door"]
        self.cfg, self.reader, self.db, self.state, self.line = d, reader, db, state, line
        self.camera = reader.cam_id
        self.device = cfg["model"]["device"]
        self.backend = str(cfg["model"].get("backend") or "ultralytics").lower()
        # the door may run its own (smaller) HEF; without one it shares the main model's HEF
        door_hef = d.get("hef") if d.get("hef") and Path(d["hef"]).is_file() else None
        self.det_cfg = {**cfg["model"], "hef": door_hef or cfg["model"].get("hef"),
                        "labels": d.get("labels") or cfg["model"].get("labels")}
        self.video_width = min(960, int(cfg["dashboard"]["video_width"]))
        self.show_video = bool(cfg["dashboard"]["show_video"])
        self.counter = CrossingCounter()
        self._stop_event = threading.Event()
        self._set_inside = None
        self._lock = threading.Lock()
        hh, mm = (int(v) for v in str(d["reset_time"]).split(":"))
        self.reset_hm = (hh, mm)

        # restore today's figures
        self.period_start = self._period_start(datetime.now())
        saved_period = db.get_state("door_period")
        if saved_period == self.period_start.strftime("%Y-%m-%d %H:%M:%S"):
            self.inside = int(db.get_state("door_inside", 0) or 0)
        else:
            self.inside = 0
        self.n_in, self.n_out = db.door_counts_since(self.period_start.strftime("%Y-%m-%d %H:%M:%S"))
        self._save()
        self._publish(active=False, fps=0.0, last_event=None)

    # -------------------------------------------------------------- helpers
    def _period_start(self, now):
        start = now.replace(hour=self.reset_hm[0], minute=self.reset_hm[1], second=0, microsecond=0)
        return start if now >= start else start - timedelta(days=1)

    def _save(self):
        self.db.set_state("door_period", self.period_start.strftime("%Y-%m-%d %H:%M:%S"))
        self.db.set_state("door_inside", self.inside)

    def _publish(self, **extra):
        self.state.update_door(enabled=True, camera=self.camera, inside=self.inside,
                               n_in=self.n_in, n_out=self.n_out,
                               since=self.period_start.strftime("%Y-%m-%d %H:%M"), **extra)

    def set_inside(self, value):
        """Called from the dashboard to correct the people-inside figure."""
        with self._lock:
            self._set_inside = max(0, int(value))

    def stop(self):
        self._stop_event.set()

    def _thumbnail(self, frame, people):
        scale = min(1.0, self.video_width / frame.shape[1])
        img = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) \
            if scale < 1.0 else frame.copy()
        for (x1, y1, x2, y2, tid) in people:
            p1, p2 = (int(x1 * scale), int(y1 * scale)), (int(x2 * scale), int(y2 * scale))
            cv2.rectangle(img, p1, p2, (100, 230, 100), 2)
            cv2.circle(img, (int((x1 + x2) / 2 * scale), p2[1]), 5, (60, 60, 255), -1)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if ok:
            self.state.set_jpeg("door", buf.tobytes())

    # ------------------------------------------------------------------ run
    def run(self):
        # Ultralytics (PC): model.track with its ByteTrack, as before. Hailo (Raspberry Pi):
        # the shared HEF detector + ByteTrackLite, no torch needed.
        model = tracker_lite = detector = None
        try:
            if self.backend == "ultralytics":
                from ultralytics import YOLO
                model = YOLO(self.cfg["weights"])
                names = model.names
            else:
                from .bytetrack import ByteTrackLite
                from .detector import make_detector
                detector = make_detector(self.det_cfg)
                names = detector.names
                tracker_lite = ByteTrackLite(high=float(self.cfg["conf"]), low=0.1,
                                             max_lost=int(max(1, float(self.cfg["fps"])) * 6))
            person_ids, note = person_class_ids(names, self.cfg.get("classes") or ["person"])
        except (ValueError, ImportError, RuntimeError) as exc:
            print(f"[door] {exc}; door counter stopped")
            self.db.write_event("door", f"door counter stopped: {exc}", "warning")
            return
        self.reader.enable_fast(self.cfg["fps"])
        prev_small, last_activity, last_thumb = None, 0.0, 0.0
        minute, min_in, min_out = None, 0, 0
        frames, fps_t0, fps, was_active, last_event = 0, time.time(), 0.0, False, None

        while not self._stop_event.is_set():
            item = self.reader.take_fast(timeout=1.0)
            now_dt = datetime.now()

            # corrections from the dashboard and the daily reset
            with self._lock:
                forced, self._set_inside = self._set_inside, None
            if forced is not None:
                self.inside = forced
                self._save()
            period = self._period_start(now_dt)
            if period != self.period_start:
                self.period_start, self.inside, self.n_in, self.n_out = period, 0, 0, 0
                self._save()

            # one row per minute, also for quiet minutes
            this_minute = now_dt.replace(second=0, microsecond=0)
            if minute is None:
                minute = this_minute
            elif this_minute != minute:
                self.db.write_door_minute(minute.strftime("%Y-%m-%d %H:%M:%S"), self.camera,
                                          min_in, min_out, self.inside)
                minute, min_in, min_out = this_minute, 0, 0

            if item is None:
                self._publish(active=False, fps=0.0, last_event=last_event)
                continue
            frame, ts = item
            h, w = frame.shape[:2]

            # cheap motion gate on a small grey picture
            small = cv2.cvtColor(cv2.resize(frame, (320, max(1, round(320 * h / w)))),
                                 cv2.COLOR_BGR2GRAY)
            small = cv2.GaussianBlur(small, (5, 5), 0)
            if prev_small is not None and prev_small.shape == small.shape:
                changed = cv2.countNonZero(cv2.threshold(cv2.absdiff(small, prev_small), 18, 255,
                                                         cv2.THRESH_BINARY)[1]) / small.size
                if changed > 0.002:
                    last_activity = ts
            else:
                last_activity = ts
            prev_small = small
            active = ts - last_activity < HOLD_SECONDS

            people = []
            if active:
                if model is not None:
                    result = model.track(frame, persist=True, tracker="bytetrack.yaml",
                                         classes=person_ids, conf=float(self.cfg["conf"]),
                                         imgsz=int(self.cfg["imgsz"]), device=self.device,
                                         verbose=False)[0]
                    rb = result.boxes
                    xyxy = rb.xyxy.tolist() if rb is not None else []
                    ids = (rb.id.int().tolist() if rb.id is not None else [None] * len(rb)) if rb is not None else []
                else:
                    xyxy, confs = detector.predict([frame], 0.1, person_ids)[0]
                    ids = tracker_lite.update(xyxy, confs)
                    keep = [i for i, t in enumerate(ids) if t is not None]
                    xyxy, ids = [xyxy[i] for i in keep], [ids[i] for i in keep]
                if xyxy:
                    last_activity = ts                 # a person in view keeps it awake
                    a, b, inside_sign = self.line.pixels(w, h)
                    margin = MARGIN_FRACTION * math.hypot(w, h)
                    for (x1, y1, x2, y2), tid in zip(xyxy, ids):
                        people.append((x1, y1, x2, y2, tid))
                        if tid is None:
                            continue
                        event = self.counter.update(tid, ((x1 + x2) / 2, y2), ts, a, b,
                                                    inside_sign, margin)
                        if event:
                            stamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
                            if event == "in":
                                self.inside += 1; self.n_in += 1; min_in += 1
                            else:
                                self.inside = max(0, self.inside - 1); self.n_out += 1; min_out += 1
                            self.db.write_door_event(stamp, self.camera, event, tid)
                            self._save()
                            last_event = {"ts": stamp, "direction": event}
                            print(f"  [door] {stamp}  {event.upper():<3}  inside now: {self.inside}")
                self.counter.forget_old(ts)
                frames += 1
            elif was_active:
                # nobody around any more: start the next visitor with a clean tracker
                self.counter.clear()
                if tracker_lite is not None:
                    tracker_lite.reset()
                trackers = getattr(getattr(model, "predictor", None), "trackers", None) or []
                for tracker in trackers:
                    if hasattr(tracker, "reset"):
                        tracker.reset()
            was_active = active

            if time.time() - fps_t0 >= 5:
                fps, frames, fps_t0 = frames / (time.time() - fps_t0), 0, time.time()
            if self.show_video and ts - last_thumb >= (0.4 if active else 2.0):
                self._thumbnail(frame, people)
                last_thumb = ts
            self._publish(active=active, fps=round(fps, 1), last_event=last_event,
                          people_in_view=len(people))

        if minute is not None:
            self.db.write_door_minute(minute.strftime("%Y-%m-%d %H:%M:%S"), self.camera,
                                      min_in, min_out, self.inside)
