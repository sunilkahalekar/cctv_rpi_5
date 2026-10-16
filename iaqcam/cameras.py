"""One background thread per camera: keeps the stream open and offers one sample per tick."""
import threading
import time

import cv2

STREAM_PREFIXES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://")


class Sample:
    __slots__ = ("frame", "gray", "prev_gray", "ts", "scale", "prev_ts")

    def __init__(self, frame, gray, prev_gray, ts, scale, prev_ts=None):
        self.frame = frame          # full-resolution BGR frame
        self.gray = gray            # grayscale copy used for optical flow
        self.prev_gray = prev_gray  # the same, one tick earlier (None right after connecting)
        self.ts = ts                # capture time (time.time())
        self.scale = scale          # full width / gray width
        self.prev_ts = prev_ts      # capture time of prev_gray (flow interval = ts - prev_ts)


class CameraReader(threading.Thread):
    def __init__(self, cam_id, name, source, tick_seconds=1.0, flow_width=640, location="",
                 flow_interval=0.12, open_capture=None):
        super().__init__(daemon=True, name=f"camera-{cam_id}")
        self.cam_id = cam_id
        self.cam_name = name
        self.location = location or ""
        self.source = source
        self.tick = float(tick_seconds)
        self.flow_width = int(flow_width or 0)
        # Optical flow is measured between the sampled frame and one taken `flow_interval`
        # seconds before it. A walking person moves too far in a whole second for Farneback
        # to follow (measured: 5 % of the true motion at 1 s, 97 % at 0.12 s; README).
        # 0 = the original behaviour: the frame one tick earlier.
        self.flow_interval = float(flow_interval or 0)
        self.events = None                # callable(message, level) for the event log
        self.open_capture = open_capture  # source -> capture (videocap.capture_factory); None = OpenCV
        self.is_file = isinstance(source, str) and not source.lower().startswith(STREAM_PREFIXES)
        self._lock = threading.Lock()
        self._sample = None
        self._fast = None                 # newest frame for a fast consumer (door counter)
        self._fast_event = threading.Event()
        self.fast_interval = 0.0          # seconds between fast frames; 0 = off
        self._stop_event = threading.Event()
        self.connected = False
        self.last_frame_ts = 0.0
        self.frame_size = None

    def stop(self):
        self._stop_event.set()

    def _log(self, message, level="info"):
        if self.events:
            try:
                self.events(message, level)
            except Exception:
                pass

    def take(self):
        """Newest sample not yet handed out, or None."""
        with self._lock:
            sample, self._sample = self._sample, None
        return sample

    def enable_fast(self, fps):
        """Also offer frames at `fps` through take_fast() (used by the door counter)."""
        self.fast_interval = 1.0 / max(0.5, float(fps))

    def take_fast(self, timeout=1.0):
        """Newest (frame, capture time) for the fast consumer, or None after `timeout`."""
        if not self._fast_event.wait(timeout):
            return None
        with self._lock:
            item, self._fast = self._fast, None
            self._fast_event.clear()
        return item

    def _gray(self, frame):
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        w = gray.shape[1]
        if self.flow_width and w > self.flow_width:
            h = round(gray.shape[0] * self.flow_width / w)
            gray = cv2.resize(gray, (self.flow_width, h), interpolation=cv2.INTER_AREA)
        return gray, w / gray.shape[1]

    def run(self):
        while not self._stop_event.is_set():
            if self.open_capture is not None:
                cap = self.open_capture(self.source)
            else:
                src = int(self.source) if isinstance(self.source, str) and self.source.isdigit() \
                    else self.source
                cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG) if isinstance(src, str) \
                    else cv2.VideoCapture(src)
            if not cap.isOpened():
                cap.release()
                if self.connected is not None:
                    self._log(f"{self.cam_name}: cannot connect, retrying every 10 s", "warning")
                self.connected = None              # logged once until it connects
                print(f"[{self.cam_name}] cannot connect, retrying in 10 s")
                self._stop_event.wait(10)
                continue
            print(f"[{self.cam_name}] connected")
            self._log(f"{self.cam_name}: stream connected")
            self.connected = True
            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_delay = 1.0 / fps if self.is_file and 1 <= fps <= 120 else 0.0
            prev_gray, last_ts, next_due, next_fast, failures = None, 0.0, 0.0, 0.0, 0
            pre_gray, pre_ts, pre_pts = None, 0.0, -1.0
            while not self._stop_event.is_set():
                if not cap.grab():                      # grab keeps the stream drained
                    failures += 1
                    if self.is_file and failures < 3:   # recorded file: loop it
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    break
                failures = 0
                if frame_delay:
                    time.sleep(frame_delay)
                now = time.time()
                pts = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0     # stream time, if the stream has it
                want_main = now >= next_due
                want_fast = bool(self.fast_interval) and now >= next_fast
                want_pre = (self.flow_interval > 0 and not want_main and pre_gray is None
                            and now >= next_due - self.flow_interval)
                if not (want_main or want_fast or want_pre):
                    continue
                ok, frame = cap.retrieve()
                if not ok or frame is None:
                    continue
                if want_fast:
                    with self._lock:
                        self._fast = (frame, now)
                    self._fast_event.set()
                    next_fast = now + self.fast_interval
                    self.last_frame_ts = now
                    self.frame_size = (frame.shape[1], frame.shape[0])
                if want_pre:
                    pre_gray, _ = self._gray(frame)
                    pre_ts, pre_pts = now, pts
                if not want_main:
                    continue
                gray, scale = self._gray(frame)
                if self.flow_interval > 0:
                    # flow partner: the frame kept just before; its age from the stream's own
                    # timestamps when they look sane (network delivery is bursty), else wall time
                    first, first_ts = pre_gray, pre_ts
                    if first is not None and pre_pts > 0 and pts > pre_pts and pts - pre_pts < 2.0:
                        first_ts = now - (pts - pre_pts)
                    if first is None or now - pre_ts > 4 * self.flow_interval + 1.0:
                        first = None
                    pre_gray = None
                else:
                    if now - last_ts > 2.5 * self.tick:     # gap: no flow across it
                        prev_gray = None
                    first, first_ts = prev_gray, last_ts
                with self._lock:
                    self._sample = Sample(frame, gray, first, now, scale,
                                          first_ts if first is not None else None)
                prev_gray, last_ts = gray, now
                self.last_frame_ts = now
                self.frame_size = (frame.shape[1], frame.shape[0])
                next_due = next_due + self.tick if now - next_due < self.tick else now + self.tick
            cap.release()
            self.connected = False
            if not self._stop_event.is_set():
                self._log(f"{self.cam_name}: stream lost, reconnecting", "warning")
                print(f"[{self.cam_name}] stream lost, reconnecting in 5 s")
                self._stop_event.wait(5)
