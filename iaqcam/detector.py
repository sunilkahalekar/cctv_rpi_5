"""Person detectors: one interface, two engines.

    model: backend: ultralytics   a .pt file (PC: CPU or NVIDIA GPU), or a folder exported by
                                  Ultralytics (for example yolo26s_hailo_model/)
    model: backend: hailo         a .hef file on the Raspberry Pi AI HAT+ 2 (Hailo-10H), run
                                  directly with HailoRT: no torch, no Ultralytics on the Pi

Every detector offers the same few things, so the detection loop and the door counter do not
care which engine runs:

    det.names          {class id: name}
    det.imgsz          input size of the network (pixels, the longer side)
    det.accelerator    "GPU", "CPU" or "Hailo-10H"
    det.model_name     file name shown on the dashboard
    det.default_batch(n_images)
    det.predict(images, conf, classes) -> [(boxes, confs), ...]  one pair per image,
                                           boxes = [(x1, y1, x2, y2), ...] in that image's pixels

The Hailo part decodes three output layouts, so the same code runs a model from Hailo's
Model Zoo, one compiled with `hailomz compile`, or one exported with Ultralytics:
  raw     YOLO26 NMS-free head as six tensors (3 scales x [box distances, class scores]);
          the layout of engine_hef.py of the edge_ai_aqi_event_aware project
  nms     a HEF with Hailo's NMS on the chip (one output, detections per class)
  table   one tensor of rows: [x1, y1, x2, y2, score, class] (end-to-end export) or
          [cx, cy, w, h, score per class ...] (needs NMS here)
`python tools/hailo_check.py` prints which layout a HEF has and how fast it runs.
"""
import threading
from pathlib import Path

import cv2
import numpy as np

COCO_NAMES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
    "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat", "dog",
    "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella",
    "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket", "bottle",
    "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
    "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock", "vase", "scissors",
    "teddy bear", "hair drier", "toothbrush"]

BACKENDS = ("ultralytics", "hailo")


def load_labels(spec):
    """'coco' (or empty) = the 80 COCO classes; otherwise a text file with one class name per line."""
    if not spec or str(spec).strip().lower() == "coco":
        return {i: n for i, n in enumerate(COCO_NAMES)}
    with open(spec, encoding="utf-8") as fh:
        names = [line.strip() for line in fh if line.strip()]
    if not names:
        raise ValueError(f"labels file {spec} is empty")
    return {i: n for i, n in enumerate(names)}


_CACHE = {}
_CACHE_LOCK = threading.Lock()


def make_detector(model_cfg, weights_key="weights", hef_key="hef", imgsz=None):
    """Detector for a config section ("model", or "door" which may name its own files).
    Detectors are shared by file: the door counter and the detection loop can use one HEF."""
    backend = str(model_cfg.get("backend") or "ultralytics").lower()
    if backend not in BACKENDS:
        raise ValueError(f"model.backend must be one of {', '.join(BACKENDS)} (is {backend!r})")
    if backend == "hailo":
        hef = model_cfg.get(hef_key)
        if not hef:
            raise ValueError("model.backend is hailo but no .hef file is set (model.hef)")
        if not Path(hef).is_file():
            raise ValueError(f"HEF file not found: {hef}. Copy the compiled model to the Pi "
                             "(docs/RPI5_SETUP.md, step 'Model').")
        key = ("hailo", str(Path(hef).resolve()), str(model_cfg.get("labels") or "coco"))
        with _CACHE_LOCK:
            if key not in _CACHE:
                _CACHE[key] = HailoDetector(hef, model_cfg.get("labels") or "coco",
                                            int(model_cfg.get("timeout_ms") or 10000),
                                            bool(model_cfg.get("multi_process_service")))
            return _CACHE[key]
    weights = model_cfg[weights_key]
    size = int(imgsz or model_cfg.get("imgsz") or 640)
    key = ("ultralytics", str(weights), size, str(model_cfg.get("device")))
    with _CACHE_LOCK:
        if key not in _CACHE:
            _CACHE[key] = UltralyticsDetector(weights, size, model_cfg.get("device"))
        return _CACHE[key]


# ----------------------------------------------------------------------------- Ultralytics
class UltralyticsDetector:
    backend = "ultralytics"

    def __init__(self, weights, imgsz, device=None):
        from ultralytics import YOLO
        self.model = YOLO(weights)            # downloads standard weights on first run
        self.names = dict(self.model.names)
        self.imgsz = int(imgsz)
        self.device = device
        self.model_name = Path(str(weights)).name
        on_gpu = False
        try:
            import torch
            on_gpu = torch.cuda.is_available() and str(device).lower() != "cpu"
        except Exception:
            pass
        self.accelerator = "GPU" if on_gpu else "CPU"
        self._lock = threading.Lock()

    def default_batch(self, n_images):
        return n_images if self.accelerator == "GPU" else 2

    def predict(self, images, conf, classes=None):
        with self._lock:
            results = self.model.predict(list(images), classes=classes, conf=conf, imgsz=self.imgsz,
                                         device=self.device, verbose=False)
        return [(r.boxes.xyxy.tolist(), r.boxes.conf.tolist()) for r in results]


# ----------------------------------------------------------------------------- Hailo
def letterbox(img, out_w, out_h):
    """Resize keeping the aspect ratio, pad with grey. Returns (picture, scale, dx, dy)."""
    h, w = img.shape[:2]
    r = min(out_w / w, out_h / h)
    nw, nh = max(1, int(round(w * r))), max(1, int(round(h * r)))
    dx, dy = (out_w - nw) // 2, (out_h - nh) // 2
    out = np.full((out_h, out_w, 3), 114, dtype=np.uint8)
    interp = cv2.INTER_AREA if r < 1 else cv2.INTER_LINEAR
    out[dy:dy + nh, dx:dx + nw] = cv2.resize(img, (nw, nh), interpolation=interp)
    return out, r, dx, dy


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def split_raw(outputs):
    """Sort raw HxWxC tensors into {(h, w): {"box": ..., "cls": ...}} (YOLO26 NMS-free head)."""
    groups = {}
    for a in outputs:
        a = np.asarray(a)
        if a.ndim == 4 and a.shape[0] == 1:
            a = a[0]
        if a.ndim != 3:
            raise ValueError(f"raw output with shape {a.shape}: expected HxWxC")
        groups.setdefault(a.shape[:2], []).append(a)
    scales = {}
    for hw, arrs in groups.items():
        boxes = [a for a in arrs if a.shape[2] == 4]
        rest = [a for a in arrs if a.shape[2] != 4]
        if len(boxes) != 1 or len(rest) != 1:
            raise ValueError(f"cannot tell boxes from class scores at scale {hw}: channels "
                             f"{[a.shape[2] for a in arrs]} (a model with exactly 4 classes needs a manual mapping)")
        scales[hw] = {"box": boxes[0], "cls": rest[0]}
    return scales


def decode_raw(outputs, conf, in_w, in_h, scores_are_logits=None, max_det=300):
    """YOLO26 head: score per class per cell; box = (left, top, right, bottom) distances from the
    cell centre in cells. Returns (boxes Nx4 in input pixels, scores N, class ids N).
    scores_are_logits None = decide from the values: a sigmoid inside the HEF gives only 0..1,
    raw logits of a whole picture are mostly far below 0."""
    scales = split_raw(outputs)
    if scores_are_logits is None:
        scores_are_logits = any(float(t["cls"].min()) < 0.0 for t in scales.values())
    thr = float(np.log(conf / (1.0 - conf))) if scores_are_logits else conf
    boxes, scores, ids = [], [], []
    for (h, w), t in scales.items():
        sx, sy = in_w / float(w), in_h / float(h)
        ys, xs, cs = np.nonzero(t["cls"] > thr)
        if ys.size == 0:
            continue
        raw = t["cls"][ys, xs, cs].astype(np.float64)
        s = _sigmoid(raw) if scores_are_logits else raw
        l, tp, r, b = t["box"][ys, xs].astype(np.float64).T
        cx, cy = xs + 0.5, ys + 0.5
        boxes.append(np.stack([(cx - l) * sx, (cy - tp) * sy, (cx + r) * sx, (cy + b) * sy], axis=1))
        scores.append(s)
        ids.append(cs)
    if not boxes:
        return np.zeros((0, 4)), np.zeros(0), np.zeros(0, dtype=int)
    boxes, scores, ids = np.concatenate(boxes), np.concatenate(scores), np.concatenate(ids)
    order = np.argsort(-scores)[:max_det]
    return boxes[order], scores[order], ids[order]


def parse_nms(buf, n_classes, in_w, in_h, conf):
    """Hailo on-chip NMS. Accepts the per-class list HailoRT's Python API returns
    ([array k x 5 of (y1, x1, y2, x2, score)] per class, normalised 0..1) or the flat
    NMS-by-class buffer (per class: count, then count x 5 values)."""
    boxes, scores, ids = [], [], []

    def add(c, rows):
        rows = np.asarray(rows, dtype=np.float64).reshape(-1, 5)
        for y1, x1, y2, x2, s in rows:
            if s >= conf:
                boxes.append((x1 * in_w, y1 * in_h, x2 * in_w, y2 * in_h))
                scores.append(s)
                ids.append(c)

    if isinstance(buf, (list, tuple)) and buf and isinstance(buf[0], (list, tuple)) and \
            len(buf) == 1 and len(buf[0]) == n_classes:
        buf = buf[0]                                        # batch of one
    if isinstance(buf, (list, tuple)):
        for c, rows in enumerate(buf):
            if rows is not None and np.size(rows):
                add(c, rows)
    else:
        flat = np.asarray(buf, dtype=np.float64).ravel()
        try:
            i, c = 0, 0
            while i < flat.size and c < n_classes:
                n = flat[i]
                if n < 0 or n != int(n) or i + 1 + 5 * int(n) > flat.size:
                    raise ValueError("not NMS-by-class")
                n = int(n)
                if n:
                    add(c, flat[i + 1:i + 1 + 5 * n])
                i += 1 + 5 * n
                c += 1
        except ValueError:
            # NMS-by-score layout: count, then count x (y1, x1, y2, x2, score, class)
            boxes.clear(); scores.clear(); ids.clear()
            n = flat[0] if flat.size else 0
            if n < 0 or n != int(n) or 1 + 6 * int(n) > flat.size:
                raise ValueError("NMS output is neither NMS-by-class nor NMS-by-score "
                                 "(run tools/hailo_check.py and send its output)")
            for y1, x1, y2, x2, s, c in flat[1:1 + 6 * int(n)].reshape(-1, 6):
                if s >= conf:
                    boxes.append((x1 * in_w, y1 * in_h, x2 * in_w, y2 * in_h))
                    scores.append(s)
                    ids.append(int(c))
    if not boxes:
        return np.zeros((0, 4)), np.zeros(0), np.zeros(0, dtype=int)
    return np.array(boxes), np.array(scores), np.array(ids, dtype=int)


def nms(boxes, scores, iou=0.6):
    if len(boxes) == 0:
        return np.zeros(0, dtype=int)
    xywh = [[float(x1), float(y1), float(x2 - x1), float(y2 - y1)] for x1, y1, x2, y2 in boxes]
    keep = cv2.dnn.NMSBoxes(xywh, [float(s) for s in scores], 0.0, iou)
    return np.array(keep, dtype=int).ravel()


def decode_table(a, conf, in_w, in_h):
    """One tensor of rows. 6 columns = (x1, y1, x2, y2, score, class) of an end-to-end model;
    otherwise (cx, cy, w, h, score per class) rows (or columns) that need NMS."""
    a = np.asarray(a, dtype=np.float64)
    while a.ndim > 2 and a.shape[0] == 1:
        a = a[0]
    if a.ndim != 2:
        raise ValueError(f"single output with shape {a.shape}: not a detection table")
    if a.shape[1] == 6 and a.shape[0] != 6:
        rows = a[a[:, 4] >= conf]
        b = rows[:, :4].copy()
        if b.size and b.max() <= 1.5:                       # normalised coordinates
            b[:, [0, 2]] *= in_w
            b[:, [1, 3]] *= in_h
        return b, rows[:, 4], rows[:, 5].astype(int)
    if a.shape[0] < a.shape[1]:                             # (4 + nc, N) -> (N, 4 + nc)
        a = a.T
    cls = a[:, 4:]
    ids = cls.argmax(1)
    s = cls[np.arange(len(a)), ids]
    m = s >= conf
    cx, cy, w, h = a[m, :4].T
    b = np.stack([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], axis=1) if m.any() else np.zeros((0, 4))
    s, ids = s[m], ids[m]
    keep = nms(b, s)
    return b[keep], s[keep], ids[keep]


_VDEVICE = None
_VDEVICE_LOCK = threading.Lock()


def shared_vdevice(multi_process=False):
    """One virtual device for the whole program; the scheduler shares the chip between models.
    multi_process=True: go through HailoRT's multi-process service (hailort.service), needed when
    ANOTHER program (e.g. event_aware_aqi on the same Pi) also uses the AI HAT+ 2 at the same time.
    Both programs must then set it."""
    global _VDEVICE
    with _VDEVICE_LOCK:
        if _VDEVICE is None:
            from hailo_platform import HailoSchedulingAlgorithm, VDevice
            params = VDevice.create_params()
            params.scheduling_algorithm = HailoSchedulingAlgorithm.ROUND_ROBIN
            params.group_id = "SHARED"
            if multi_process:
                params.multi_process_service = True
            _VDEVICE = VDevice(params)
        return _VDEVICE


class HailoDetector:
    backend = "hailo"
    accelerator = "Hailo-10H"

    def __init__(self, hef, labels="coco", timeout_ms=10000, multi_process=False):
        from hailo_platform import FormatType
        self.model_name = Path(hef).name
        self.timeout_ms = int(timeout_ms)
        self.names = load_labels(labels)
        try:
            vdevice = shared_vdevice(multi_process)
        except Exception as exc:
            hint = ("the AI HAT+ 2 is probably in use by another program (event_aware_aqi?): stop it, or set "
                    "model: multi_process_service: true here AND in the other program, with hailort.service "
                    "running (docs/RPI5_SETUP.md, section 0)") if not multi_process else \
                   ("multi-process service not reachable: sudo systemctl enable --now hailort.service")
            raise RuntimeError(f"cannot open the Hailo device ({type(exc).__name__}: {exc}); {hint}") from exc
        self.infer_model = vdevice.create_infer_model(str(hef))
        self.infer_model.set_batch_size(1)
        self.out_names = list(self.infer_model.output_names)
        self.is_nms = False
        for name in self.out_names:
            out = self.infer_model.output(name)
            if getattr(out, "is_nms", False):
                self.is_nms = True
            try:
                out.set_format_type(FormatType.FLOAT32)        # de-quantised floats
            except Exception:
                pass
        shape = tuple(self.infer_model.input().shape)          # (H, W, 3)
        self.in_h, self.in_w = int(shape[0]), int(shape[1])
        self.imgsz = max(self.in_h, self.in_w)
        self._ctx = self.infer_model.configure()
        self.configured = self._ctx.__enter__()
        self._lock = threading.Lock()
        self.layout = "nms" if self.is_nms else ("table" if len(self.out_names) == 1 else "raw")

    def default_batch(self, n_images):
        return max(1, n_images)

    def describe(self):
        outs = []
        for name in self.out_names:
            o = self.infer_model.output(name)
            outs.append(f"{name} shape={tuple(o.shape)} nms={bool(getattr(o, 'is_nms', False))}")
        return {"hef": self.model_name, "input": (self.in_h, self.in_w, 3), "layout": self.layout,
                "outputs": outs, "classes": len(self.names)}

    def _run(self, rgb):
        bindings = self.configured.create_bindings()
        bindings.input().set_buffer(rgb)
        buffers = {}
        for name in self.out_names:
            buf = np.empty(self.infer_model.output(name).shape, dtype=np.float32)
            bindings.output(name).set_buffer(buf)
            buffers[name] = buf
        self.configured.run([bindings], self.timeout_ms)
        if self.is_nms:
            return {name: bindings.output(name).get_buffer() for name in self.out_names}
        return buffers

    def decode(self, outs, conf):
        if self.layout == "nms":
            return parse_nms(next(iter(outs.values())), len(self.names), self.in_w, self.in_h, conf)
        if self.layout == "table":
            return decode_table(next(iter(outs.values())), conf, self.in_w, self.in_h)
        return decode_raw(list(outs.values()), conf, self.in_w, self.in_h)

    def predict(self, images, conf, classes=None):
        wanted = None if classes is None else set(int(c) for c in classes)
        results = []
        for img in images:
            lb, r, dx, dy = letterbox(img, self.in_w, self.in_h)
            rgb = np.ascontiguousarray(cv2.cvtColor(lb, cv2.COLOR_BGR2RGB))
            with self._lock:
                outs = self._run(rgb)
            boxes, scores, ids = self.decode(outs, conf)
            results.append(unletterbox(boxes, scores, ids, r, dx, dy, img.shape[1], img.shape[0], wanted))
        return results


def unletterbox(boxes, scores, ids, r, dx, dy, w, h, wanted=None):
    """Boxes from network-input pixels back to the picture's pixels, filtered by class."""
    out_b, out_s = [], []
    for (x1, y1, x2, y2), s, c in zip(boxes, scores, ids):
        if wanted is not None and int(c) not in wanted:
            continue
        bx = (float(np.clip((x1 - dx) / r, 0, w)), float(np.clip((y1 - dy) / r, 0, h)),
              float(np.clip((x2 - dx) / r, 0, w)), float(np.clip((y2 - dy) / r, 0, h)))
        if bx[2] - bx[0] < 1 or bx[3] - bx[1] < 1:
            continue
        out_b.append(bx)
        out_s.append(float(s))
    return out_b, out_s
