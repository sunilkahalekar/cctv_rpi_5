"""Small ByteTrack-style tracker for the door counter when the detector is not Ultralytics
(the Pi has no torch, so Ultralytics' own tracker is not available there).

ByteTrack's idea in a few lines: match the confident boxes to the known persons first, then
let the weak boxes continue persons that are still unmatched (a person half hidden by the door
frame keeps his number), and start new persons only from confident boxes. A person is moved
by his last velocity before matching, so walking people at 5 pictures per second are followed.
"""
import numpy as np

try:
    import lap
except Exception:                       # optional: greedy matching without it
    lap = None


def iou_matrix(a, b):
    if not len(a) or not len(b):
        return np.zeros((len(a), len(b)))
    a = np.asarray(a, dtype=np.float64)[:, None, :]
    b = np.asarray(b, dtype=np.float64)[None, :, :]
    ix1, iy1 = np.maximum(a[..., 0], b[..., 0]), np.maximum(a[..., 1], b[..., 1])
    ix2, iy2 = np.minimum(a[..., 2], b[..., 2]), np.minimum(a[..., 3], b[..., 3])
    inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
    area_a = (a[..., 2] - a[..., 0]) * (a[..., 3] - a[..., 1])
    area_b = (b[..., 2] - b[..., 0]) * (b[..., 3] - b[..., 1])
    return inter / np.maximum(area_a + area_b - inter, 1e-9)


def match(cost, limit):
    """Pairs (row, col) with cost below limit, at the lowest total cost."""
    if cost.size == 0:
        return []
    if lap is not None:
        _, x, _ = lap.lapjv(cost, extend_cost=True, cost_limit=limit)
        return [(i, int(j)) for i, j in enumerate(x) if j >= 0]
    pairs, used_r, used_c = [], set(), set()
    for flat in np.argsort(cost, axis=None):
        i, j = divmod(int(flat), cost.shape[1])
        if cost[i, j] >= limit:
            break
        if i not in used_r and j not in used_c:
            pairs.append((i, j))
            used_r.add(i)
            used_c.add(j)
    return pairs


class _Track:
    __slots__ = ("tid", "box", "vel", "lost", "hits")

    def __init__(self, tid, box):
        self.tid, self.box, self.vel, self.lost, self.hits = tid, np.asarray(box, float), np.zeros(4), 0, 1

    def predicted(self):
        return self.box + self.vel * (1 + self.lost)

    def update(self, box):
        box = np.asarray(box, float)
        self.vel = 0.5 * self.vel + 0.5 * (box - self.box) / (1 + self.lost)
        self.box, self.lost, self.hits = box, 0, self.hits + 1


class ByteTrackLite:
    def __init__(self, high=0.3, low=0.1, new_track=None, match_iou=0.2, max_lost=30):
        self.high, self.low = float(high), float(low)
        self.new_track = float(new_track if new_track is not None else high)
        self.limit = 1.0 - float(match_iou)
        self.max_lost = int(max_lost)
        self.tracks = []
        self.next_id = 1

    def reset(self):
        self.tracks = []

    def update(self, boxes, confs):
        """boxes [(x1, y1, x2, y2)], confs [..] -> track id per box (None: weak box nobody continues)."""
        ids = [None] * len(boxes)
        hi = [i for i, c in enumerate(confs) if c >= self.high]
        lo = [i for i, c in enumerate(confs) if self.low <= c < self.high]
        pred = [t.predicted() for t in self.tracks]
        free_t = list(range(len(self.tracks)))
        for group in (hi, lo):
            if not group or not free_t:
                continue
            cost = 1.0 - iou_matrix([pred[t] for t in free_t], [boxes[i] for i in group])
            taken = set()
            for r, c in match(cost, self.limit):
                t = self.tracks[free_t[r]]
                t.update(boxes[group[c]])
                ids[group[c]] = t.tid
                taken.add(free_t[r])
            free_t = [t for t in free_t if t not in taken]
        for t in free_t:
            self.tracks[t].lost += 1
        for i in hi:
            if ids[i] is None and confs[i] >= self.new_track:
                t = _Track(self.next_id, boxes[i])
                self.next_id += 1
                self.tracks.append(t)
                ids[i] = t.tid
        self.tracks = [t for t in self.tracks if t.lost <= self.max_lost]
        return ids
