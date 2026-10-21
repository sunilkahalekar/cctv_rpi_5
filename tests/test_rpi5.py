"""Raspberry Pi 5 system without the Pi: Hailo decoding (also through a stand-in for HailoRT's
Python API), the door tracker, the ffmpeg video reader, the ESP32-only fan controller and the
Pi configuration file."""
import socket
import subprocess
import sys
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

import cv2
import numpy as np
import pytest

from iaqcam import detector as D
from iaqcam.bytetrack import ByteTrackLite
from iaqcam.config import load_config
from iaqcam.fan_controller import Esp32FanController, FanError, make_controller

APP = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- YOLO26 raw head
def raw_outputs(boxes_px, cls_ids, size=640, nc=80, logits=True):
    """Six tensors as a YOLO26 NMS-free head gives them, with one confident cell per box."""
    outs = []
    for stride in (8, 16, 32):
        g = size // stride
        box = np.zeros((g, g, 4), np.float32)
        cls = np.full((g, g, nc), -12.0 if logits else 0.0, np.float32)
        if stride == 16:
            for (x1, y1, x2, y2), c in zip(boxes_px, cls_ids):
                cx, cy = (x1 + x2) / 2 / stride, (y1 + y2) / 2 / stride
                gx, gy = int(cx), int(cy)
                ccx, ccy = gx + 0.5, gy + 0.5
                box[gy, gx] = [ccx - x1 / stride, ccy - y1 / stride, x2 / stride - ccx, y2 / stride - ccy]
                cls[gy, gx, c] = 3.0 if logits else 0.95
        outs += [box, cls]
    return outs


def test_decode_raw_logits_and_probabilities():
    truth = [(100, 120, 180, 400), (300, 200, 360, 330)]
    for logits in (True, False):
        b, s, ids = D.decode_raw(raw_outputs(truth, [0, 56], logits=logits), 0.25, 640, 640)
        assert len(b) == 2 and set(ids.tolist()) == {0, 56}
        got = sorted(map(tuple, np.round(b).astype(int).tolist()))
        assert got == sorted(truth)
        assert s.min() > 0.9


def test_letterbox_roundtrip():
    img = np.zeros((360, 640, 3), np.uint8)                  # a sub-stream picture
    lb, r, dx, dy = D.letterbox(img, 640, 640)
    assert lb.shape == (640, 640, 3) and dy == 140 and dx == 0
    boxes = np.array([[100 * r + dx, 50 * r + dy, 200 * r + dx, 300 * r + dy]])
    out_b, out_s = D.unletterbox(boxes, [0.9], [0], r, dx, dy, 640, 360, {0})
    assert np.allclose(out_b[0], (100, 50, 200, 300), atol=0.5)
    assert D.unletterbox(boxes, [0.9], [3], r, dx, dy, 640, 360, {0}) == ([], [])


def test_parse_nms_list_and_flat():
    per_class = [np.zeros((0, 5))] * 80
    per_class[0] = np.array([[0.1, 0.2, 0.5, 0.4, 0.8], [0.0, 0.0, 0.1, 0.1, 0.1]])
    b, s, ids = D.parse_nms(per_class, 80, 640, 640, 0.25)
    assert len(b) == 1 and np.allclose(b[0], (128, 64, 256, 320)) and ids[0] == 0
    flat = [1, 0.1, 0.2, 0.5, 0.4, 0.8] + [0] * 79
    b2, _, _ = D.parse_nms(np.array(flat, np.float32), 80, 640, 640, 0.25)
    assert np.allclose(b2, b)
    with pytest.raises(ValueError):
        D.parse_nms(np.array([2.5, 1, 2], np.float32), 80, 640, 640, 0.25)


def test_decode_table_end2end_and_cxcywh():
    rows = np.zeros((300, 6), np.float32)
    rows[0] = [10, 20, 110, 220, 0.9, 0]
    rows[1] = [0, 0, 5, 5, 0.1, 0]
    b, s, ids = D.decode_table(rows[None], 0.25, 640, 640)
    assert len(b) == 1 and np.allclose(b[0], (10, 20, 110, 220))
    t = np.zeros((4 + 80, 8400), np.float32)                  # (4 + nc, N), two copies of one box
    t[:4, 0] = t[:4, 1] = [60, 120, 100, 200]
    t[4, 0], t[4, 1] = 0.9, 0.85
    b, s, ids = D.decode_table(t[None], 0.25, 640, 640)
    assert len(b) == 1 and np.allclose(b[0], (10, 20, 110, 220))   # NMS kept one


def test_make_detector_errors(tmp_path):
    with pytest.raises(ValueError):
        D.make_detector({"backend": "tpu"})
    with pytest.raises(ValueError):
        D.make_detector({"backend": "hailo", "hef": None})
    with pytest.raises(ValueError, match="not found"):
        D.make_detector({"backend": "hailo", "hef": str(tmp_path / "x.hef")})
    lab = tmp_path / "labels.txt"
    lab.write_text("student\nteacher\n")
    assert D.load_labels(str(lab)) == {0: "student", 1: "teacher"}
    assert D.load_labels("coco")[0] == "person"


# ---------------------------------------------------------------- HailoRT stand-in
class _Stream:
    def __init__(self, shape):
        self.shape, self.is_nms = shape, False

    def set_format_type(self, t):
        pass


class _Binding:
    def __init__(self):
        self.buf = None

    def set_buffer(self, b):
        self.buf = b

    def get_buffer(self):
        return self.buf


class _Bindings:
    def __init__(self, names):
        self._in, self._out = _Binding(), {n: _Binding() for n in names}

    def input(self):
        return self._in

    def output(self, name=None):
        return self._out[name]


class _Configured:
    def __init__(self, model):
        self.model = model

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def create_bindings(self):
        return _Bindings(self.model.output_names)

    def run(self, bindings, timeout):
        for bd in bindings:
            assert bd.input().buf.shape == (640, 640, 3) and bd.input().buf.dtype == np.uint8
            self.model.inputs_seen.append(bd.input().buf)
            for name, arr in zip(self.model.output_names, self.model.answer):
                bd.output(name).buf[...] = arr


class _InferModel:
    def __init__(self, answer):
        self.answer = answer
        self.output_names = [f"out{i}" for i in range(len(answer))]
        self.inputs_seen = []

    def set_batch_size(self, n):
        pass

    def output(self, name):
        return _Stream(self.answer[self.output_names.index(name)].shape)

    def input(self):
        return _Stream((640, 640, 3))

    def configure(self):
        return _Configured(self)


def fake_hailo(monkeypatch, answer):
    model = _InferModel(answer)

    class VDevice:
        def __init__(self, params):
            pass

        @staticmethod
        def create_params():
            return types.SimpleNamespace()

        def create_infer_model(self, hef):
            return model
    mod = types.SimpleNamespace(VDevice=VDevice, HailoSchedulingAlgorithm=types.SimpleNamespace(ROUND_ROBIN=1),
                                FormatType=types.SimpleNamespace(FLOAT32=1))
    monkeypatch.setitem(sys.modules, "hailo_platform", mod)
    monkeypatch.setattr(D, "_VDEVICE", None)
    D._CACHE.clear()
    return model


def test_hailo_detector_through_api_stand_in(monkeypatch, tmp_path):
    # person at (100..180, 200..400) in the 640x640 letterboxed input of a 1280x720 picture
    model = fake_hailo(monkeypatch, raw_outputs([(100, 200, 180, 400)], [0]))
    hef = tmp_path / "yolo26s.hef"
    hef.write_bytes(b"x")
    det = D.make_detector({"backend": "hailo", "hef": str(hef), "labels": "coco"})
    assert det.accelerator == "Hailo-10H" and det.imgsz == 640 and det.layout == "raw"
    assert D.make_detector({"backend": "hailo", "hef": str(hef), "labels": "coco"}) is det   # shared
    frame = np.zeros((720, 1280, 3), np.uint8)
    (boxes, confs), = det.predict([frame], 0.25, [0])
    r, dy = 0.5, 140                                         # 1280x720 -> 640x360, 140 px grey band
    assert len(boxes) == 1 and np.allclose(boxes[0], (200, (200 - dy) / r, 360, (400 - dy) / r), atol=1)
    assert det.predict([frame], 0.25, [56]) == [([], [])]   # other class only
    assert len(model.inputs_seen) == 2


def test_pipeline_runs_on_hailo_backend(monkeypatch, tmp_path):
    """The real detection loop (Pipeline._process) with the Hailo detector stand-in."""
    from iaqcam.cameras import Sample
    from iaqcam.db import Database
    from iaqcam.pipeline import LiveState, Pipeline
    fake_hailo(monkeypatch, raw_outputs([(100, 300, 180, 600)], [0]))
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "yolo26s.hef").write_bytes(b"x")
    text = (APP / "config.yaml").read_text(encoding="utf-8")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(text, encoding="utf-8")
    cfg = load_config(cfg_path)
    cfg["setup"]["require_setup"] = False
    cfg["setup"]["detect_in"] = "picture"
    cfg["cameras"] = cfg["cameras"][:1]
    cam = types.SimpleNamespace(cam_id=1, cam_name="Camera 1", location="L1")
    db = Database(str(tmp_path / "t.db"))
    state = LiveState([cam])
    pipe = Pipeline(cfg, [cam], db, state, roi=None)
    assert pipe.accelerator == "Hailo-10H" and pipe.imgsz == 640
    frame = np.full((640, 640, 3), 90, np.uint8)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    now = time.time()
    sec, _, persons = pipe._process([(cam, Sample(frame, gray, gray, now, 1.0, now - 0.1))])
    assert sec[0][4] == 1                                   # one person detected
    db.close()


# ---------------------------------------------------------------- door tracker
def test_bytetrack_lite_follows_walking_person_and_weak_boxes():
    t = ByteTrackLite(high=0.3, low=0.1)
    ids = [t.update([(100 + 25 * k, 100, 160 + 25 * k, 300)], [0.8])[0] for k in range(10)]
    assert ids[0] is not None and len(set(ids)) == 1
    # half hidden in the door frame: weak box continues him, a weak box elsewhere starts nobody
    out = t.update([(100 + 25 * 10, 100, 160 + 25 * 10, 300), (600, 100, 650, 300)], [0.15, 0.15])
    assert out[0] == ids[0] and out[1] is None
    # a second, confident person gets a new number
    out = t.update([(100 + 25 * 11, 100, 160 + 25 * 11, 300), (600, 100, 650, 300)], [0.8, 0.7])
    assert out[0] == ids[0] and out[1] not in (None, ids[0])
    t.reset()
    assert t.update([(0, 0, 10, 10)], [0.9])[0] not in ids


# ---------------------------------------------------------------- ffmpeg reader
def test_ffmpeg_capture_reads_scaled_frames_at_fps(tmp_path):
    from iaqcam.videocap import FfmpegCapture, capture_factory, out_size
    path = str(tmp_path / "v.mp4")
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 25, (1280, 720))
    for i in range(50):                                       # 2 s at 25 fps
        f = np.zeros((720, 1280, 3), np.uint8)
        f[:, (i * 20) % 1280:(i * 20) % 1280 + 40] = 255
        w.write(f)
    w.release()
    cap = FfmpegCapture(path, decode_fps=10, decode_width=640, hwaccel="auto")
    assert cap.isOpened() and (cap.w, cap.h) == (640, 360)
    n = 0
    while cap.grab():
        ok, frame = cap.retrieve()
        assert ok and frame.shape == (360, 640, 3)
        n += 1
    cap.release()
    assert 18 <= n <= 22                                      # 2 s at 10 pictures per second
    assert abs(cap.get(cv2.CAP_PROP_POS_MSEC) - n * 100) < 1
    assert out_size(704, 576, 0) == (704, 576)
    # a recorded file stays with OpenCV even when the decoder is ffmpeg (only streams use ffmpeg)
    assert isinstance(capture_factory({"decoder": "ffmpeg"})(path), cv2.VideoCapture)


def test_hw_args():
    from iaqcam.videocap import hw_args
    assert hw_args("hevc", "auto") == ["-hwaccel", "drm"]
    assert hw_args("h264", "auto") == []
    assert hw_args("hevc", "none") == []


# ---------------------------------------------------------------- configuration
def test_rpi5_config_loads_and_validates(tmp_path):
    cfg = load_config(APP / "config.yaml")
    assert cfg["model"]["backend"] == "hailo" and cfg["model"]["hef"].endswith("yolo26s.hef")
    assert cfg["fan"]["controller"] == "esp32" and cfg["fan"]["dry_run"] is True
    assert cfg["pipeline"]["decoder"] == "ffmpeg" and cfg["nvr"]["stream_index"] == 2
    assert abs(cfg["pipeline"]["flow_interval"] - 1 / cfg["pipeline"]["decode_fps"]) < 1e-6
    text = (APP / "config.yaml").read_text(encoding="utf-8")
    bad = tmp_path / "c.yaml"
    bad.write_text(text.replace("dry_run: true ", "dry_run: false ").replace(
        'url: "http://10.185.103.170:8095"', 'url: ""'), encoding="utf-8")
    with pytest.raises(ValueError, match="esp32.url"):
        load_config(bad)
    fast = tmp_path / "f.yaml"                                # flow faster than the pictures: corrected
    fast.write_text(text.replace("flow_interval: 0.1 ", "flow_interval: 0.05"), encoding="utf-8")
    assert load_config(fast)["pipeline"]["flow_interval"] == 0.1


# ---------------------------------------------------------------- ESP32 only fan
def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_esp32_only_controller_against_agent(tmp_path):
    """tests/fan_agent_mock/agent.py (the Pi 4 agent in --mock mode) speaks the same API as esp32_fan_agent.ino: it stands in for the ESP32."""
    port, token = free_port(), "esp-token-1"
    proc = subprocess.Popen([sys.executable, str(APP / "tests" / "fan_agent_mock" / "agent.py"), "--mock", "--port", str(port),
                             "--host", "127.0.0.1", "--token-file", str(tmp_path / "tok.txt"),
                             "--log", str(tmp_path / "agent.log")],
                            env={"FAN_AGENT_TOKEN": token, "PATH": "/usr/bin:/bin"})
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "/status", timeout=0.2)
            except urllib.error.HTTPError:
                break
            except OSError:
                time.sleep(0.1)
        fan_cfg = {"controller": "esp32", "esp32": {"url": url, "timeout_seconds": 1, "retries": 0}}
        ctl = make_controller(fan_cfg, tokens={"esp32": token})
        assert isinstance(ctl, Esp32FanController) and ctl.kind == "esp32"
        ctl.initialize()
        st = ctl.fan_on("test ON")
        assert st["requested"] is True and st["active"] == "esp32" and "ESP32 agent" in st["controller"]
        assert ctl.status()["active"] == "esp32"
        assert ctl.fan_off("test OFF")["requested"] is False
        with pytest.raises(FanError, match="ESP32"):
            Esp32FanController(url, "", 1, 0).initialize()
    finally:
        proc.terminate()
        proc.wait(5)


# ---------------------------------------------------------------- CPU fallback
def test_ultralytics_fallback_backend(monkeypatch, tmp_path):
    """model.backend: ultralytics (a .pt on the CPU, e.g. before the .hef exists): same interface."""
    from iaqcam.cameras import Sample
    from iaqcam.db import Database
    from iaqcam.pipeline import LiveState, Pipeline

    class _T(list):
        def tolist(self):
            return list(self)

    class FakeYOLO:
        names = {0: "person", 1: "bicycle"}
        calls = []

        def __init__(self, weights):
            self.weights = weights

        def predict(self, images, classes=None, conf=0.25, imgsz=640, device=None, verbose=False):
            FakeYOLO.calls.append((len(images), classes, imgsz))
            return [types.SimpleNamespace(boxes=types.SimpleNamespace(
                xyxy=_T([[100.0, 300.0, 180.0, 600.0]]), conf=_T([0.9]))) for _ in images]
    monkeypatch.setitem(sys.modules, "ultralytics", types.SimpleNamespace(YOLO=FakeYOLO))
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False)))
    D._CACHE.clear()
    cfg = load_config(APP / "config.yaml")
    cfg["model"].update(backend="ultralytics", weights="yolo26s.pt", imgsz=960, batch=0)
    cfg["setup"]["require_setup"] = False
    cfg["setup"]["detect_in"] = "picture"
    cam = types.SimpleNamespace(cam_id=1, cam_name="Camera 1", location="L1")
    db = Database(str(tmp_path / "t.db"))
    pipe = Pipeline(cfg, [cam], db, LiveState([cam]), roi=None)
    assert pipe.accelerator == "CPU" and pipe.imgsz == 960 and pipe.batch == 2
    frame = np.full((720, 1280, 3), 90, np.uint8)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    now = time.time()
    sec, _, _ = pipe._process([(cam, Sample(frame, gray, gray, now, 1.0, now - 0.1))])
    assert sec[0][4] == 1 and FakeYOLO.calls[-1] == (1, [0], 960)
    db.close()


def test_hailo_busy_message_and_multi_process(monkeypatch, tmp_path):
    seen = {}

    class VDevice:
        def __init__(self, params):
            seen["mps"] = getattr(params, "multi_process_service", False)
            raise RuntimeError("HAILO_OUT_OF_PHYSICAL_DEVICES")

        @staticmethod
        def create_params():
            return types.SimpleNamespace()
    mod = types.SimpleNamespace(VDevice=VDevice, HailoSchedulingAlgorithm=types.SimpleNamespace(ROUND_ROBIN=1),
                                FormatType=types.SimpleNamespace(FLOAT32=1))
    monkeypatch.setitem(sys.modules, "hailo_platform", mod)
    monkeypatch.setattr(D, "_VDEVICE", None)
    D._CACHE.clear()
    hef = tmp_path / "m.hef"
    hef.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="multi_process_service"):
        D.make_detector({"backend": "hailo", "hef": str(hef)})
    assert seen["mps"] is False
    D._CACHE.clear()
    with pytest.raises(RuntimeError, match="hailort.service"):
        D.make_detector({"backend": "hailo", "hef": str(hef), "multi_process_service": True})
    assert seen["mps"] is True


# ---------------------------------------------------------------- long-term detection archive
def test_archive_seconds_writes_each_finished_day_once(tmp_path):
    import csv
    import gzip
    from iaqcam.db import Database
    db = Database(str(tmp_path / "iaq_cctv.db"))
    rows, persons = [], []
    for day in ("2026-10-19", "2026-10-20"):
        for s in range(30):
            ts = f"{day} 10:00:{s:02d}"
            rows.append((ts, 8, 2, 0.1, 2, 1, 0.3))
            persons += [(ts, 8, g, 0.9, 1, 1.5, 0.01, 0.02, 0, 10, 20, 50, 200, 0.01, 0.02) for g in (1, 2)]
    db.write_seconds(rows)
    db.write_person_obs(persons)
    db.close()
    out = tmp_path / "archive"
    cmd = [sys.executable, str(APP / "tools" / "archive_seconds.py"), "--db", str(tmp_path / "iaq_cctv.db"),
           "--out", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    with gzip.open(out / "2026-10-19" / "person_obs.csv.gz", "rt") as fh:
        data = list(csv.reader(fh))
    assert data[0][:3] == ["id", "Timestamp", "camera"] and len(data) == 61
    with gzip.open(out / "2026-10-20" / "camera_second.csv.gz", "rt") as fh:
        assert len(list(csv.reader(fh))) == 31
    again = subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    assert "already archived" in again
