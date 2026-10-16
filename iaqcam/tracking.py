"""
Person tracking per camera: every detected person gets an ID that stays with them while
they move, so that motion and speed can be followed person by person.

The cameras are sampled every few seconds, not 25 times a second, so a tracker made for
video (which expects a person to move only a few pixels between frames) is not used.
Instead each new detection is matched to the known persons by

  * overlap with where the person is expected to be (last position + their velocity),
  * distance of the feet from that expected position, allowing more room the longer
    the gap, and
  * appearance: the colours of the upper and lower half of the body,

and the best overall pairing is chosen. A person who disappears (hidden behind a
machine, out of view for a moment) is recognised again by appearance for up to
`reid_seconds` and keeps their ID, like the StableIDRegistry of iaq-edge-pipeline.
IDs are per camera: the same person seen by two cameras gets two IDs.

Made for a lab where people mostly sit and work and sometimes walk (version 7):

  * Two confidence levels. A box above `model.conf` may start a new person; a weaker box
    (down to `tracking.conf_low`) may only continue a person already known. A person far
    from the camera whose confidence drops for a picture keeps their ID instead of
    flickering out.
  * Confirmation. A new ID counts only after `min_hits` pictures (or one very confident
    picture), so a single false box never adds a person.
  * Hold. A person who was sitting / standing still inside the area and is missed by YOLO
    (hidden by a monitor, bent over the bench, far away) stays present with the same ID
    for `hold_seconds`. A person who was walking when lost is held only
    `exit_hold_seconds` (they most likely left); one standing still at the edge of the
    area `edge_hold_seconds`. A held person found again,
    at the same place, gets the same ID.
  * Duplicates. Two boxes on one person (a whole body and a part of it) are merged, so a
    person never gets two IDs; two people side by side keep their own boxes and IDs.

Speed is how far the feet moved since the person's previous picture, in body heights
per second, so that a person far from the camera and one close to it give comparable
values (walking is roughly 0.5-1 body heights per second, sitting close to 0).
"""
import math

import cv2
import lap
import numpy as np

W_IOU, W_NEAR, W_LOOK = 0.40, 0.30, 0.30
MIN_SCORE = 0.30          # weaker pairings are not accepted
REID_LOOK = 0.85          # appearance similarity needed to recognise a lost person
MAX_SPEED = 3.0           # body heights per second; faster is treated as a mismatch
PREDICT_SECONDS = 3.0     # velocity is used to predict a position for at most this long
DUP_IOU = 0.75            # two boxes overlapping this much are the same person
CROWDED_IOU = 0.3         # a box overlapping another this much is ambiguous: when two people
                          # cross, their boxes (and colours) mix, so such a match updates the
                          # position but not the person's velocity or colour signature


def appearance(frame, box):
    """Colour signature of a person box (hue/saturation of upper and lower half)."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = max(0, int(box[0])), max(0, int(box[1])), min(w, int(box[2])), min(h, int(box[3]))
    if x2 - x1 < 4 or y2 - y1 < 8:
        return None
    crop = cv2.resize(frame[y1:y2, x1:x2], (24, 48), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    parts = [cv2.calcHist([half], [0, 1], None, [16, 4], [0, 180, 0, 256]).ravel()
             for half in (hsv[:24], hsv[24:])]
    vec = np.concatenate(parts)
    norm = np.linalg.norm(vec)
    return vec / norm if norm > 0 else None


def iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def foot(box):
    return ((box[0] + box[2]) / 2.0, float(box[3]))


def dedupe(boxes, confs):
    """Indexes of the boxes to keep: of two boxes on the same person (strong overlap, or a
    box inside another with the same feet and centre, e.g. upper body + whole body) the
    more confident one stays. Two people side by side are kept apart."""
    order = sorted(range(len(boxes)), key=lambda i: -confs[i])
    keep = []
    for i in order:
        b = boxes[i]
        same = False
        for k in keep:
            o = boxes[k]
            if iou(b, o) >= DUP_IOU:
                same = True
                break
            ix = max(0.0, min(b[2], o[2]) - max(b[0], o[0]))
            iy = max(0.0, min(b[3], o[3]) - max(b[1], o[1]))
            small = min((b[2] - b[0]) * (b[3] - b[1]), (o[2] - o[0]) * (o[3] - o[1]))
            if small <= 0 or ix * iy / small < 0.85:
                continue
            width = max(b[2] - b[0], o[2] - o[0])
            height = max(b[3] - b[1], o[3] - o[1])
            centred = abs((b[0] + b[2]) - (o[0] + o[2])) / 2.0
            if (centred < 0.25 * width and abs(b[3] - o[3]) < 0.25 * height) \
                    or (centred < 0.2 * width and ix * iy / small >= 0.92):
                same = True
                break
        if not same:
            keep.append(i)
    return sorted(keep)


class Track:
    __slots__ = ("gid", "seq", "box", "t", "vel", "look", "height", "hits", "confirmed",
                 "inside", "edge", "walking")

    def __init__(self, gid, seq, box, t, look):
        self.gid, self.seq, self.box, self.t, self.look = gid, seq, box, t, look
        self.vel = (0.0, 0.0)                     # feet velocity, pixels per second
        self.height = max(1.0, box[3] - box[1])
        self.hits = 1
        self.confirmed = False
        self.inside = False                       # feet inside the detection area when last seen
        self.edge = True                          # near the edge of the area / picture
        self.walking = False                      # feet moving when last seen


class PersonTracker:
    def __init__(self, cam_id, next_seq=1, max_gap=20.0, reid_seconds=90.0, hold_seconds=90.0,
                 exit_hold_seconds=5.0, edge_hold_seconds=30.0, min_hits=2, conf_new=0.25, confirm_conf=0.5,
                 walk_speed=0.3):
        self.cam_id = int(cam_id)
        self.next_seq = int(next_seq)
        self.max_gap = float(max_gap)
        self.hold_seconds = float(hold_seconds)
        self.exit_hold_seconds = float(exit_hold_seconds)
        self.edge_hold_seconds = float(edge_hold_seconds)
        self.reid_seconds = max(float(reid_seconds), self.hold_seconds)
        self.min_hits = max(1, int(min_hits))
        self.conf_new = float(conf_new)
        self.confirm_conf = float(confirm_conf)
        self.walk_speed = float(walk_speed)
        self.tracks = []
        self.held = []          # confirmed persons not seen in the last picture but still present
        self._by_gid = {}

    def _new(self, box, t, look):
        seq = self.next_seq
        self.next_seq += 1
        track = Track(self.cam_id * 1_000_000 + seq, seq, box, t, look)
        self.tracks.append(track)
        self._by_gid[track.gid] = track
        return track

    # ------------------------------------------------------------ state for counting
    def confirmed(self, gid):
        tr = self._by_gid.get(gid)
        return bool(tr and tr.confirmed)

    def mark(self, gid, inside, near_edge, speed):
        """Where the person stands after this picture (from the detection area) and whether
        they walk: decides how long they are held if YOLO misses them."""
        tr = self._by_gid.get(gid)
        if tr is None:
            return
        tr.inside, tr.edge = bool(inside), bool(near_edge)
        if speed is not None:
            tr.walking = speed >= self.walk_speed

    def hold_limit(self, tr):
        if not tr.inside:
            return 0.0
        if tr.walking:
            return self.exit_hold_seconds
        return min(self.edge_hold_seconds, self.hold_seconds) if tr.edge else self.hold_seconds

    def _held(self, t, seen):
        return [tr for tr in self.tracks
                if tr.confirmed and tr.gid not in seen and 0 < t - tr.t <= self.hold_limit(tr)]

    @staticmethod
    def _update(track, box, t, look, measure_speed, crowded=False):
        dt = t - track.t
        f0, f1 = foot(track.box), foot(box)
        height = (track.height + max(1.0, box[3] - box[1])) / 2.0
        speed = None
        if measure_speed and dt > 0:
            disp = math.hypot(f1[0] - f0[0], f1[1] - f0[1])
            speed = disp / height / dt
            if not crowded:
                v = ((f1[0] - f0[0]) / dt, (f1[1] - f0[1]) / dt)
                track.vel = (0.5 * track.vel[0] + 0.5 * v[0], 0.5 * track.vel[1] + 0.5 * v[1])
        elif not crowded:
            track.vel = (0.0, 0.0)
        if look is not None and not crowded:
            track.look = look if track.look is None else 0.7 * track.look + 0.3 * look
        track.box, track.t, track.height = box, t, max(1.0, box[3] - box[1])
        return speed

    def update(self, boxes, frame, t, confs=None):
        """boxes: [(x1, y1, x2, y2)] in pixels; t: capture time in seconds; confs: YOLO
        confidence per box (None = all strong). Returns one (global id, sequence number,
        speed or None) per box, or None for a weak box that matches nobody (dropped).
        Afterwards self.held lists the persons held present without a box."""
        confs = list(confs) if confs is not None else [1.0] * len(boxes)
        looks = [appearance(frame, b) for b in boxes]
        crowded = [any(iou(b, o) > CROWDED_IOU for k, o in enumerate(boxes) if k != j) for j, b in enumerate(boxes)]
        # unconfirmed IDs are forgotten quickly; confirmed ones are kept for re-identification
        self.tracks = [tr for tr in self.tracks
                       if t - tr.t <= (self.reid_seconds if tr.confirmed else self.max_gap)]
        self._by_gid = {tr.gid: tr for tr in self.tracks}
        active = [tr for tr in self.tracks if t - tr.t <= max(self.max_gap, self.hold_limit(tr))]
        result = [None] * len(boxes)

        if active and boxes:
            cost = np.full((len(active), len(boxes)), 1e6)
            for i, tr in enumerate(active):
                dt = max(0.0, t - tr.t)
                ahead = min(dt, PREDICT_SECONDS)       # a person missed for long is looked for where last seen
                vx = max(-MAX_SPEED * tr.height, min(MAX_SPEED * tr.height, tr.vel[0]))
                vy = max(-MAX_SPEED * tr.height, min(MAX_SPEED * tr.height, tr.vel[1]))
                pred = (tr.box[0] + vx * ahead, tr.box[1] + vy * ahead, tr.box[2] + vx * ahead, tr.box[3] + vy * ahead)
                pf = foot(pred)
                # room to move grows with the gap, but not without limit (seated people stay put)
                gate = tr.height * (0.6 + 1.2 * min(dt, 2.0 if not tr.walking else self.max_gap))
                for j, b in enumerate(boxes):
                    ratio = (b[3] - b[1]) / tr.height
                    if not 0.5 <= ratio <= 2.0:
                        continue
                    f = foot(b)
                    near = max(0.0, 1.0 - math.hypot(f[0] - pf[0], f[1] - pf[1]) / gate)
                    overlap = iou(pred, b)
                    if near == 0.0 and overlap == 0.0:
                        continue
                    look = float(np.dot(tr.look, looks[j])) if tr.look is not None and looks[j] is not None else 0.5
                    score = W_IOU * overlap + W_NEAR * near + W_LOOK * max(0.0, look)
                    if score >= MIN_SCORE:
                        # strong boxes are preferred; a weak one only fills in
                        cost[i, j] = 1.0 - score + (0.05 if confs[j] < self.conf_new else 0.0)
            _, rows, _ = lap.lapjv(cost, extend_cost=True, cost_limit=1.05 - MIN_SCORE)
            for i, j in enumerate(rows):
                if j >= 0 and cost[i, j] < 1e5:
                    tr = active[i]
                    speed = self._update(tr, boxes[j], t, looks[j], True, crowded[j])
                    if crowded[j]:
                        speed = None           # may belong to the other person: not measured
                    result[j] = (tr.gid, tr.seq, speed)

        # not matched: a person seen before (lost for a while) or a new one
        taken = {r[0] for r in result if r}
        for j, b in enumerate(boxes):
            if result[j] is not None:
                continue
            if confs[j] < self.conf_new:
                continue                      # weak box: may only continue a known person
            best, best_look = None, REID_LOOK
            if looks[j] is not None:
                for tr in self.tracks:
                    if tr.gid in taken or tr.look is None or t - tr.t <= 0:
                        continue
                    ratio = (b[3] - b[1]) / tr.height
                    if not 0.5 <= ratio <= 2.0:
                        continue
                    look = float(np.dot(tr.look, looks[j]))
                    if look >= best_look:
                        best, best_look = tr, look
            if best is not None:
                self._update(best, b, t, looks[j], False, crowded[j])
                track = best
            else:
                track = self._new(b, t, looks[j])
                track.hits = 0
            taken.add(track.gid)
            result[j] = (track.gid, track.seq, None)

        for j, r in enumerate(result):
            if r is None:
                continue
            tr = self._by_gid[r[0]]
            if tr.t == t:
                tr.hits += 1
                if tr.hits >= self.min_hits or confs[j] >= self.confirm_conf:
                    tr.confirmed = True
        self.held = self._held(t, taken)
        return result
