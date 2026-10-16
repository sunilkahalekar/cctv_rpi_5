"""
Camera setup: the detection area (region of interest) of every camera.

Set up first, detect after. A camera is analysed only after it has been set up on the
dashboard (page "Setup"). Each camera has one of these modes:

  setup  not set up yet: no YOLO, nothing is stored, the dashboard asks for its setup
  area   persons are detected inside the drawn areas only (polygons, several allowed)
  full   the whole picture is the detection area
  skip   set up as "not analysed" (e.g. a camera that shows only a corridor)

In "area" mode YOLO looks only at the part of the picture around the areas (the crop
also keeps some room above the areas for the body of a person standing at the back edge
of an area). A person counts when the bottom-centre of their box (where they stand) is
inside an enabled area. Persons found in the crop but outside the areas keep their ID
(no new ID when they step in) but never count: not for occupancy, motion, variance,
the forecast or the fan.

The areas can be changed at any time; the change applies from the next picture on and
is written to the event log. Points are stored normalised (0..1 of the picture), so they
stay valid if the camera resolution changes. Saved in roi.json next to config.yaml.
"""
import json
import os
import threading
import time
from pathlib import Path

MAX_POLYGONS = 8
MAX_POINTS = 40
MODES = ("area", "full", "skip")
ANALYSED = ("area", "full")


def point_in_polygon(x, y, pts):
    """Even-odd ray casting; points on an edge may fall either way (irrelevant here)."""
    inside = False
    n = len(pts)
    j = n - 1
    for i in range(n):
        xi, yi = pts[i]
        xj, yj = pts[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def validate(polygons):
    """Clean list of {name, enabled, points}; raises ValueError with a readable reason."""
    if not isinstance(polygons, list) or len(polygons) > MAX_POLYGONS:
        raise ValueError(f"at most {MAX_POLYGONS} areas per camera")
    out = []
    for i, poly in enumerate(polygons):
        if not isinstance(poly, dict):
            raise ValueError("each area must be an object")
        pts = poly.get("points")
        if not isinstance(pts, list) or not 3 <= len(pts) <= MAX_POINTS:
            raise ValueError(f"an area needs 3 to {MAX_POINTS} points")
        clean = []
        for p in pts:
            if not (isinstance(p, (list, tuple)) and len(p) == 2):
                raise ValueError("points must be [x, y]")
            x, y = float(p[0]), float(p[1])
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ValueError("points must lie inside the picture (0..1)")
            clean.append([round(x, 4), round(y, 4)])
        name = str(poly.get("name") or f"Area {i + 1}")[:40]
        out.append({"name": name, "enabled": bool(poly.get("enabled", True)), "points": clean})
    return out


def validate_setup(entry):
    """Clean {mode, areas, updated, by}. Accepts the old format (a plain list of areas)."""
    if isinstance(entry, list):                       # roi.json of v6
        areas = validate(entry)
        return {"mode": "area" if any(a["enabled"] for a in areas) else "full", "areas": areas,
                "updated": None, "by": None}
    if not isinstance(entry, dict):
        raise ValueError("camera setup must be an object")
    mode = entry.get("mode")
    if mode not in MODES:
        raise ValueError("mode must be one of: " + ", ".join(MODES))
    areas = validate(entry.get("areas") or [])
    if mode == "area" and not any(a["enabled"] for a in areas):
        raise ValueError("draw at least one enabled area, or choose 'whole picture' or 'not analysed'")
    return {"mode": mode, "areas": areas, "updated": entry.get("updated"), "by": entry.get("by")}


class RoiStore:
    """Thread-safe camera setups. require_setup=False: a camera that was never set up is
    analysed over the whole picture (behaviour of v6)."""

    def __init__(self, path, defaults=None, require_setup=True, crop_margin=0.04, head_room=0.35):
        self.path = Path(path)
        self.require_setup = bool(require_setup)
        self.crop_margin = float(crop_margin)
        self.head_room = float(head_room)
        self._lock = threading.RLock()
        self._data = {}
        self._rev = 0
        loaded = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"[setup] {self.path.name} could not be read ({exc}): every camera needs its setup")
        elif defaults:
            loaded = defaults
        for cam, entry in (loaded or {}).items():
            try:
                self._data[int(cam)] = validate_setup(entry)
            except (ValueError, TypeError) as exc:
                print(f"[setup] setup of camera {cam} ignored: {exc}")

    # ----------------------------------------------------------------- reading
    @property
    def revision(self):
        """Changes whenever any setup is saved."""
        return self._rev

    def mode(self, cam):
        with self._lock:
            entry = self._data.get(int(cam))
        if entry is None:
            return "setup" if self.require_setup else "full"
        return entry["mode"]

    def analysed(self, cam):
        return self.mode(cam) in ANALYSED

    def setup(self, cam):
        """Full setup of one camera for the dashboard."""
        with self._lock:
            entry = self._data.get(int(cam))
            areas = [dict(p, points=[list(q) for q in p["points"]]) for p in entry["areas"]] if entry else []
            return {"mode": self.mode(cam), "areas": areas, "configured": entry is not None,
                    "updated": entry and entry["updated"], "by": entry and entry["by"]}

    def get(self, cam):
        """The drawn areas (also kept while the mode is 'full' or 'skip')."""
        return self.setup(cam)["areas"]

    def all(self):
        with self._lock:
            return {cam: dict(e) for cam, e in self._data.items()}

    def summary(self, cam_ids):
        """Setup progress over the given cameras."""
        modes = {c: self.mode(c) for c in cam_ids}
        return {"modes": modes,
                "pending": [c for c, m in modes.items() if m == "setup"],
                "analysed": [c for c, m in modes.items() if m in ANALYSED],
                "done": sum(1 for m in modes.values() if m != "setup"), "total": len(modes)}

    def active(self, cam):
        """Enabled polygons when the camera uses areas, else []."""
        with self._lock:
            entry = self._data.get(int(cam))
            if not entry or entry["mode"] != "area":
                return []
            return [p["points"] for p in entry["areas"] if p["enabled"]]

    def contains(self, cam, x_norm, y_norm, polys=None):
        """True when the point counts: inside an enabled area, or anywhere with 'full'."""
        if polys is None:
            mode = self.mode(cam)
            if mode == "full":
                return True
            if mode != "area":
                return False
            polys = self.active(cam)
        return any(point_in_polygon(x_norm, y_norm, pts) for pts in polys)

    def classify(self, cam, boxes, width, height):
        """Counts-flag per box, by the bottom-centre point of the box."""
        mode = self.mode(cam)
        if mode == "full":
            return [True] * len(boxes)
        polys = self.active(cam) if mode == "area" else []
        if not polys:
            return [False] * len(boxes)
        return [self.contains(cam, ((b[0] + b[2]) / 2.0) / width, float(b[3]) / height, polys)
                for b in boxes]

    def crop(self, cam, width, height):
        """Pixel rectangle (x1, y1, x2, y2) YOLO looks at, or None for the whole picture.
        Around the enabled areas, with a margin, and head room above them so that a
        person whose feet stand at the back edge of an area is seen entirely."""
        polys = self.active(cam)
        if not polys:
            return None
        xs = [p[0] for pts in polys for p in pts]
        ys = [p[1] for pts in polys for p in pts]
        x1 = max(0.0, min(xs) - self.crop_margin)
        x2 = min(1.0, max(xs) + self.crop_margin)
        y1 = max(0.0, min(ys) - self.head_room)
        y2 = min(1.0, max(ys) + self.crop_margin)
        box = (int(x1 * width), int(y1 * height), int(round(x2 * width)), int(round(y2 * height)))
        if (box[2] - box[0]) * (box[3] - box[1]) > 0.9 * width * height:
            return None                       # nearly everything: no gain from cropping
        return box

    def crops(self, cam, width, height, per_area=True):
        """Rectangles YOLO looks at. per_area: one per enabled area (a small area far from
        the camera is then enlarged to the model's input size, so distant persons are big
        enough to be found); rectangles that overlap a lot are joined. [] = whole picture."""
        polys = self.active(cam)
        if not polys:
            return []
        if not per_area:
            box = self.crop(cam, width, height)
            return [box] if box else []
        rects = [self._rect([pts], width, height) for pts in polys]
        merged = True
        while merged and len(rects) > 1:
            merged = False
            for i in range(len(rects)):
                for j in range(i + 1, len(rects)):
                    a, b = rects[i], rects[j]
                    u = (min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3]))
                    area = lambda r: max(0, r[2] - r[0]) * max(0, r[3] - r[1])
                    if area(u) <= 1.25 * (area(a) + area(b)):   # joining costs little
                        rects[i] = u
                        del rects[j]
                        merged = True
                        break
                if merged:
                    break
        if len(rects) == 1 and (rects[0][2] - rects[0][0]) * (rects[0][3] - rects[0][1]) > 0.9 * width * height:
            return []
        return rects

    def _rect(self, polys, width, height):
        xs = [p[0] for pts in polys for p in pts]
        ys = [p[1] for pts in polys for p in pts]
        x1 = max(0.0, min(xs) - self.crop_margin)
        x2 = min(1.0, max(xs) + self.crop_margin)
        y1 = max(0.0, min(ys) - self.head_room)
        y2 = min(1.0, max(ys) + self.crop_margin)
        return (int(x1 * width), int(y1 * height), int(round(x2 * width)), int(round(y2 * height)))

    def near_edge(self, cam, box, width, height, fraction=0.3):
        """True when the feet of the box are close to the edge of the detection area (or of
        the picture with 'whole picture'): a person lost there has probably left."""
        fx, fy = (box[0] + box[2]) / 2.0, float(box[3])
        room = fraction * max(1.0, box[3] - box[1])
        if self.mode(cam) != "area":
            return min(fx, width - fx, height - fy) < room
        best = None
        for pts in self.active(cam):
            px = [(x * width, y * height) for x, y in pts]
            for k in range(len(px)):
                (x1, y1), (x2, y2) = px[k - 1], px[k]
                dx, dy = x2 - x1, y2 - y1
                L = dx * dx + dy * dy
                u = 0.0 if L == 0 else max(0.0, min(1.0, ((fx - x1) * dx + (fy - y1) * dy) / L))
                d = ((fx - x1 - u * dx) ** 2 + (fy - y1 - u * dy) ** 2) ** 0.5
                best = d if best is None else min(best, d)
        return best is not None and best < room

    # ----------------------------------------------------------------- writing
    def set(self, cam, polygons, mode=None, user=None):
        """Save the setup of a camera. mode None = 'area' if an enabled area exists, else 'full'
        (the call of v6). Returns the stored setup."""
        areas = validate(polygons or [])
        if mode is None:
            mode = "area" if any(a["enabled"] for a in areas) else "full"
        entry = validate_setup({"mode": mode, "areas": areas,
                                "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "by": user})
        with self._lock:
            self._data[int(cam)] = entry
            self._rev += 1
            self._write()
        return self.setup(cam)

    def reset(self, cam):
        """Back to 'not set up' (the camera stops being analysed when setup is required)."""
        with self._lock:
            self._data.pop(int(cam), None)
            self._rev += 1
            self._write()

    def _write(self):
        data = {str(k): v for k, v in sorted(self._data.items())}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)
