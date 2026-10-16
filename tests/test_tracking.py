"""Person IDs stay with people who move, cross each other and disappear for a while."""
import numpy as np

from iaqcam.tracking import PersonTracker

COLORS = [(200, 30, 30), (30, 200, 30), (30, 30, 200), (200, 200, 30)]


def frame_with(boxes_colors, h=720, w=1280):
    frame = np.full((h, w, 3), 90, np.uint8)
    for (x1, y1, x2, y2), c in boxes_colors:
        frame[int(y1):int(y1) + 110, int(x1):int(x2)] = c
        frame[int(y1) + 110:int(y2), int(x1):int(x2)] = tuple(255 - v for v in c)
    return frame


def test_ids_stable_through_crossing_and_hiding():
    rng = np.random.default_rng(0)
    start = [(100, 300), (400, 320), (700, 280), (1000, 330)]
    vel = [(12, 0), (6, 2), (0, 0), (-15, -2)]       # persons 0 and 3 cross person 2
    tr = PersonTracker(8, 1, max_gap=20, reid_seconds=90)
    seen = [set() for _ in start]
    for k in range(24):
        t = 5.0 * k
        boxes, who, paint = [], [], []
        for p, ((x, y), (vx, vy)) in enumerate(zip(start, vel)):
            if p == 1 and 8 <= k <= 11:              # hidden for 20 s
                continue
            cx, cy = x + vx * t, y + vy * t
            boxes.append((cx, cy, cx + 80, cy + 220)); who.append(p); paint.append(((cx, cy, cx + 80, cy + 220), COLORS[p]))
        order = rng.permutation(len(boxes))
        boxes = [boxes[i] for i in order]; who = [who[i] for i in order]
        for (gid, seq, speed), p in zip(tr.update(boxes, frame_with(paint), t), who):
            seen[p].add(gid)
    assert all(len(s) == 1 for s in seen)
    assert len(set().union(*seen)) == 4
    assert all(g // 1_000_000 == 8 for s in seen for g in s)       # camera-specific IDs


def test_speed_in_body_heights_per_second():
    tr = PersonTracker(1)
    out = None
    for k in range(3):
        box = (100 + 50 * k, 200, 180, 400)       # 200 px tall, 50 px every 5 s
        box = (box[0], box[1], box[0] + 80, box[3])
        out = tr.update([box], frame_with([(box, COLORS[0])]), 5.0 * k)
    assert abs(out[0][2] - 50 / 200 / 5) < 1e-6


def test_numbers_continue_after_restart():
    tr = PersonTracker(3, next_seq=41)
    gid, seq, _ = tr.update([(10, 10, 60, 160)], frame_with([((10, 10, 60, 160), COLORS[1])]), 0.0)[0]
    assert seq == 41 and gid == 3_000_041


def _seat(tr, t, boxes, confs=None):
    paint = [(b, COLORS[i % 4]) for i, b in enumerate(boxes)]
    return tr.update(boxes, frame_with(paint), t, confs)


def test_seated_person_missed_is_held_with_same_id():
    tr = PersonTracker(1, hold_seconds=90, exit_hold_seconds=5)
    seat = (500, 300, 580, 520)
    gid = None
    for k in range(3):
        gid = _seat(tr, float(k), [seat])[0][0]
        tr.mark(gid, inside=True, near_edge=False, speed=0.0)
    assert tr.confirmed(gid)
    for k in range(3, 63):                      # YOLO misses them for a minute
        _seat(tr, float(k), [])
        assert [h.gid for h in tr.held] == [gid]
    assert _seat(tr, 63.0, [(503, 302, 582, 522)])[0][0] == gid      # same ID again
    for k in range(64, 64 + 95):
        _seat(tr, float(k), [])
    assert tr.held == []                        # gone after hold_seconds


def test_walking_person_lost_is_not_held_long():
    tr = PersonTracker(1, hold_seconds=90, exit_hold_seconds=5)
    for k in range(3):
        box = (100 + 60 * k, 300, 180 + 60 * k, 520)
        gid = _seat(tr, float(k), [box])[0][0]
        tr.mark(gid, inside=True, near_edge=False, speed=0.5)
    _seat(tr, 4.0, [])
    assert len(tr.held) == 1
    _seat(tr, 9.0, [])
    assert tr.held == []


def test_new_person_needs_two_pictures_and_weak_box_only_continues():
    tr = PersonTracker(1, conf_new=0.25, confirm_conf=0.5, min_hits=2)
    gid = _seat(tr, 0.0, [(100, 100, 180, 320)], [0.3])[0][0]
    assert not tr.confirmed(gid)
    assert _seat(tr, 1.0, [(101, 100, 181, 320)], [0.3])[0][0] == gid and tr.confirmed(gid)
    # far-away person: confidence drops to 0.12 -> same person continues
    assert _seat(tr, 2.0, [(102, 101, 182, 321)], [0.12])[0][0] == gid
    # a weak box where nobody was -> dropped, no new ID
    assert _seat(tr, 3.0, [(102, 101, 182, 321), (900, 100, 980, 320)], [0.4, 0.12])[1] is None


def test_two_people_side_by_side_keep_their_ids():
    tr = PersonTracker(1)
    a0, b0 = (400, 300, 480, 520), (455, 305, 535, 525)       # sitting shoulder to shoulder
    ids = None
    for k in range(30):
        jit = (k % 3) - 1
        boxes = [(a0[0] + jit, a0[1], a0[2] + jit, a0[3]), (b0[0] - jit, b0[1], b0[2] - jit, b0[3])]
        if k % 2:
            boxes = boxes[::-1]
        out = _seat(tr, float(k), boxes)
        got = {tuple(b): r[0] for b, r in zip(boxes, out)}
        pair = (got[boxes[0] if not k % 2 else boxes[1]], got[boxes[1] if not k % 2 else boxes[0]])
        ids = ids or pair
        assert pair == ids and ids[0] != ids[1]


def test_dedupe_whole_body_and_part():
    from iaqcam.tracking import dedupe
    whole, upper, other = (100, 100, 200, 400), (115, 100, 190, 250), (190, 110, 290, 400)
    assert dedupe([whole, upper, other], [0.9, 0.5, 0.8]) == [0, 2]
    assert dedupe([whole, (102, 101, 199, 398)], [0.6, 0.7]) == [1]
