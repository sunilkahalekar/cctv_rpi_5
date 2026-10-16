"""Video input for the camera threads.

    pipeline: decoder: opencv   cv2.VideoCapture (its built-in FFmpeg, software decoding).
                                The PC default.
    pipeline: decoder: ffmpeg   one `ffmpeg` process per camera (Raspberry Pi 5). Decoding runs
                                in its own process on any free core, H.265 streams can use the
                                Pi 5's hardware decoder (hwaccel), and ffmpeg hands over only
                                `decode_fps` pictures per second, already scaled to
                                `decode_width`, so Python never touches the other frames.

FfmpegCapture has the few methods of cv2.VideoCapture that CameraReader uses (isOpened, grab,
retrieve, get, set, release), so the camera thread is the same for both decoders.
"""
import json
import os
import select
import shutil
import subprocess
import time

import cv2
import numpy as np

STREAM_PREFIXES = ("rtsp://", "rtsps://", "rtmp://", "http://", "https://")
READ_TIMEOUT = 10.0          # seconds without a picture = stream lost, reconnect


def is_stream(source):
    return isinstance(source, str) and source.lower().startswith(STREAM_PREFIXES)


def probe(url, timeout=20):
    """{'codec': 'h264'|'hevc'|..., 'width': .., 'height': .., 'fps': ..} or None."""
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    cmd = [exe, "-v", "error"] + rtsp_args(url) + ["-select_streams", "v:0",
           "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
           "-of", "json", url]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True).stdout
        st = (json.loads(out or "{}").get("streams") or [None])[0]
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None
    if not st or not st.get("width"):
        return None

    def rate(text):
        try:
            a, b = str(text).split("/")
            return float(a) / float(b) if float(b) else 0.0
        except (ValueError, ZeroDivisionError):
            return 0.0
    return {"codec": st.get("codec_name"), "width": int(st["width"]), "height": int(st["height"]),
            "fps": rate(st.get("avg_frame_rate")) or rate(st.get("r_frame_rate"))}


def rtsp_args(url):
    return ["-rtsp_transport", "tcp"] if str(url).lower().startswith("rtsp") else []


def hw_args(codec, hwaccel):
    """Raspberry Pi 5: hardware decoding exists for H.265 (HEVC) only, through DRM.
    auto = use it for H.265, software for everything else."""
    hwaccel = str(hwaccel or "none").lower()
    if hwaccel == "none":
        return []
    if hwaccel == "auto":
        return ["-hwaccel", "drm"] if codec in ("hevc", "h265") else []
    return ["-hwaccel", hwaccel]


def out_size(width, height, decode_width):
    if decode_width and width > decode_width:
        h = int(round(height * decode_width / width / 2.0)) * 2
        return int(decode_width), max(2, h)
    return width, height


class FfmpegCapture:
    def __init__(self, url, decode_fps=10, decode_width=0, hwaccel="auto", info=None):
        self.url = url
        self.fps = float(decode_fps or 0)
        self.proc = None
        self.frame = None
        self.count = 0
        self.info = info or probe(url)
        if not self.info:
            return
        self.w, self.h = out_size(self.info["width"], self.info["height"], int(decode_width or 0))
        self.size = self.w * self.h * 3
        vf = []
        if self.fps > 0:
            vf.append(f"fps={self.fps:g}")
        if (self.w, self.h) != (self.info["width"], self.info["height"]):
            vf.append(f"scale={self.w}:{self.h}:flags=area")
        self.cmd = ([shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
                    + (rtsp_args(url) or ["-re"])          # not RTSP (a test stream of a file): own speed
                    + ["-fflags", "nobuffer", "-flags", "low_delay"]
                    + hw_args(self.info.get("codec"), hwaccel)
                    + ["-i", url, "-an", "-sn"]
                    + (["-vf", ",".join(vf)] if vf else [])
                    + ["-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1"])
        try:
            self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         bufsize=self.size * 2)
        except OSError:
            self.proc = None
        self._buf = bytearray(self.size)

    def isOpened(self):
        return self.proc is not None and self.proc.poll() is None

    def _read_exact(self):
        view, got, fd = memoryview(self._buf), 0, self.proc.stdout.fileno()
        deadline = time.time() + READ_TIMEOUT
        while got < self.size:
            left = deadline - time.time()
            if left <= 0:
                return False
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                return False
            n = os.readv(fd, [view[got:]]) if hasattr(os, "readv") else None
            if n is None:
                chunk = os.read(fd, self.size - got)
                n = len(chunk)
                view[got:got + n] = chunk
            if n == 0:                      # ffmpeg ended
                return False
            got += n
        return True

    def grab(self):
        if not self.isOpened():
            return False
        if not self._read_exact():
            return False
        self.count += 1
        return True

    def retrieve(self):
        if self.count == 0:
            return False, None
        return True, np.frombuffer(self._buf, dtype=np.uint8).reshape(self.h, self.w, 3).copy()

    def read(self):
        return self.retrieve() if self.grab() else (False, None)

    def get(self, prop):
        if prop == cv2.CAP_PROP_POS_MSEC:
            return self.count / self.fps * 1000.0 if self.fps > 0 else 0.0
        if prop == cv2.CAP_PROP_FPS:
            return self.fps or float((self.info or {}).get("fps") or 0.0)
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(getattr(self, "w", 0))
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(getattr(self, "h", 0))
        return 0.0

    def set(self, prop, value):
        return False

    def release(self):
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
            self.proc = None


def capture_factory(pipeline_cfg):
    """Function source -> capture object, chosen by pipeline.decoder."""
    decoder = str(pipeline_cfg.get("decoder") or "opencv").lower()

    def open_capture(source):
        if decoder == "ffmpeg" and is_stream(source):
            return FfmpegCapture(source, pipeline_cfg.get("decode_fps", 10),
                                 pipeline_cfg.get("decode_width", 0), pipeline_cfg.get("hwaccel", "auto"))
        src = int(source) if isinstance(source, str) and source.isdigit() else source
        return cv2.VideoCapture(src, cv2.CAP_FFMPEG) if isinstance(src, str) else cv2.VideoCapture(src)
    return open_capture
