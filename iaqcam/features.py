"""
Motion and window features, ported from iaq-edge-pipeline (iaq_pipeline_pi.py).

compute_frame_motion() and compute_Ht() are the same functions as in that
repository; FARNEBACK_PARAMS are the same values. The only structural change is
MinuteWindow: windows are aligned to wall-clock minutes (one per camera) instead
of counting 60 ticks from start-up, so camera rows and sensor rows share the
same window_start. The window statistics themselves are in analytics.py.
"""
import math
from datetime import datetime, timedelta

import cv2
import numpy as np

from .analytics import window_stats

FARNEBACK_PARAMS = dict(
    pyr_scale=0.5, levels=3, winsize=15,
    iterations=3, poly_n=5, poly_sigma=1.2, flags=0,
)


def compute_frame_motion(fu, fv, boxes: list) -> float:
    """Mean optical-flow magnitude inside each person box, averaged over boxes."""
    if not boxes:
        return 0.0
    h, w = fu.shape
    mag = np.sqrt(fu ** 2 + fv ** 2)
    scores = []
    for (x1, y1, x2, y2) in boxes:
        r = mag[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
        if r.size:
            scores.append(float(r.mean()))
    return float(np.mean(scores)) if scores else 0.0


def compute_Ht(motion_seq: list, n_max: int) -> dict:
    """Window features: max person count, mean and (population) variance of motion."""
    arr = np.array(motion_seq, dtype=float)
    return dict(
        n_person=n_max,
        mu_motion=round(float(arr.mean()) if arr.size else 0.0, 6),
        sigma2_motion=round(float(arr.var()) if arr.size else 0.0, 6),
    )


def box_motions(prev_gray, gray, boxes, scale=1.0) -> list:
    """
    Motion of each person box: Farneback flow between the previous sample and this
    one, mean magnitude inside the box (None when the box has no pixels, or when
    there is no previous frame).

    `gray`/`prev_gray` may be down-scaled copies of the frame; `scale` is
    full_width / gray_width. Boxes are given in full-resolution pixels and the
    flow is multiplied by `scale`, so the values stay in full-resolution pixels.
    """
    if prev_gray is None or not boxes or prev_gray.shape != gray.shape:
        return [None] * len(boxes)
    flow = cv2.calcOpticalFlowFarneback(prev_gray, gray, None, **FARNEBACK_PARAMS)
    mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2) * scale
    h, w = mag.shape
    out = []
    for (x1, y1, x2, y2) in boxes:
        if scale != 1.0:
            x1, y1 = int(math.floor(x1 / scale)), int(math.floor(y1 / scale))
            x2, y2 = int(math.ceil(x2 / scale)), int(math.ceil(y2 / scale))
        r = mag[max(0, y1):min(h, y2), max(0, x1):min(w, x2)]
        out.append(float(r.mean()) if r.size else None)
    return out


def frame_motion(per_box) -> float:
    """The original per-frame motion score: mean over the person boxes (0 without any)."""
    vals = [v for v in per_box if v is not None]
    return float(np.mean(vals)) if vals else 0.0


def motion_score(prev_gray, gray, boxes, scale=1.0) -> float:
    """Motion score for one sampled frame, as in the original main loop."""
    return frame_motion(box_motions(prev_gray, gray, boxes, scale))


def window_start_of(ts: datetime, window_seconds: int = 60) -> datetime:
    midnight = ts.replace(hour=0, minute=0, second=0, microsecond=0)
    offset = int((ts - midnight).total_seconds()) // window_seconds * window_seconds
    return midnight + timedelta(seconds=offset)


class MinuteWindow:
    """Collects one camera's samples for the current wall-clock window."""

    def __init__(self, window_seconds: int = 60, index_ref: float = 0.3):
        self.ws = window_seconds
        self.index_ref = index_ref
        self.start = None
        self.closed = None          # start of the last window written out
        self._reset()

    def _reset(self):
        self.samples = []           # analytics.sample_summary per picture
        self.people = []            # (global id, activity, speed) per person in the ROI
        self.legacy = []            # original frame score (pixels) per picture
        self.detected = []          # all boxes per picture, also outside the ROI

    def push(self, ts: datetime, summary: dict, people=(), legacy: float = 0.0, detected: int = 0):
        """Add a sample. Returns the finished row of the previous window, if any."""
        start = window_start_of(ts, self.ws)
        if self.closed is not None and start <= self.closed:
            return None             # late sample for a window that is already stored
        finished = None
        if self.start is not None and start != self.start:
            finished = self.flush()
        if self.start is None:
            self.start = start
        self.samples.append(summary)
        self.people.extend(people)
        self.legacy.append(legacy)
        self.detected.append(detected)
        return finished

    def flush_if_over(self, now: datetime):
        """Close the window once its time span has passed (camera may be silent)."""
        if self.start is not None and now >= self.start + timedelta(seconds=self.ws):
            return self.flush()
        return None

    def flush(self):
        if self.start is None or not self.samples:
            self.start = None
            return None
        self.closed = self.start
        row = {"window_start": self.start.strftime("%Y-%m-%d %H:%M:%S")}
        row.update(window_stats(self.samples, self.people, self.legacy, self.index_ref))
        row["n_samples"] = len(self.samples)
        row["n_detected_max"] = max(self.detected, default=0)
        self.start = None
        self._reset()
        return row
