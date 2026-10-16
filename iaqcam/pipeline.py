"""Detection loop: YOLO person detection inside each camera's detection area, tracking,
motion analytics per camera, minute windows to SQLite.

A camera is analysed only after its setup (roi.py). Cameras waiting for their setup, or
set up as 'not analysed', only deliver a plain picture for the Setup page."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import cv2

from .config import zone_of_camera
from .analytics import Rolling, Settings, motion_index, person_measure, person_stats, sample_summary
from .model_classes import person_class_ids
from .features import MinuteWindow, box_motions, frame_motion, window_start_of
from .tracking import PersonTracker, dedupe

log = logging.getLogger("iaqcam.analytics")



class LiveState:
    """Thread-safe snapshot of the latest results, read by the dashboard."""

    def __init__(self, cameras):
        self._lock = threading.Lock()
        self._readers = {c.cam_id: c for c in cameras}
        self.cameras = {c.cam_id: {"id": c.cam_id, "name": c.cam_name, "location": c.location, "count": None,
                                   "motion": None, "last_ts": None, "minute": None}
                        for c in cameras}
        self.jpeg = {}
        self.jpeg_ring = {}
        self.info = {"round_seconds": None, "started": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        self.sensors = {"last_ok": None, "last_error": None, "channel_name": None}
        self.door = {"enabled": False}
        self.forecast = {"status": "Forecast is switched off", "ready": False}

    def update_camera(self, cam_id, **values):
        with self._lock:
            self.cameras[cam_id].update(values)

    def set_jpeg(self, cam_id, data, seq=None):
        """Newest picture; the last few are kept by sequence number so that the dashboard can
        fetch exactly the picture its box overlay belongs to."""
        with self._lock:
            self.jpeg[cam_id] = data
            if seq is not None:
                ring = self.jpeg_ring.setdefault(cam_id, [])
                ring.append((seq, data))
                del ring[:-4]

    def get_jpeg(self, cam_id, seq=None):
        with self._lock:
            if seq is not None:
                for s, data in self.jpeg_ring.get(cam_id, []):
                    if s == seq:
                        return data
            return self.jpeg.get(cam_id)

    def update_info(self, **values):
        with self._lock:
            self.info.update(values)

    def update_sensors(self, **values):
        with self._lock:
            self.sensors.update(values)

    def update_door(self, **values):
        with self._lock:
            self.door.update(values)

    def update_forecast(self, **values):
        with self._lock:
            self.forecast.update(values)

    def extras(self):
        with self._lock:
            return dict(self.door), dict(self.forecast)

    def snapshot(self):
        with self._lock:
            cameras = {k: dict(v) for k, v in self.cameras.items()}
            info, sensors = dict(self.info), dict(self.sensors)
        for cam_id, cam in cameras.items():
            reader = self._readers[cam_id]
            cam["connected"] = bool(reader.connected)
            cam["stream_ts"] = reader.last_frame_ts or None    # last frame received
        return cameras, info, sensors


def _f(value, digits):
    return "  n/a" if value is None else f"{value:.{digits}f}"


class Pipeline:
    def __init__(self, cfg, cameras, db, state, roi=None):
        self.cfg = cfg
        self.cameras = cameras
        self.db = db
        self.state = state
        p = cfg["pipeline"]
        self.tick = float(p["tick_seconds"])
        self.settings = Settings(cfg)
        self.roi = roi
        self.show_outside = bool(cfg["analytics"].get("show_outside", True))
        self.windows = {c.cam_id: MinuteWindow(int(p["window_seconds"]), self.settings.index_ref)
                        for c in cameras}
        self.rolling = {c.cam_id: Rolling(float(p["window_seconds"])) for c in cameras}
        self.names = {c.cam_id: c.cam_name for c in cameras}
        self.locations = {c.cam_id: c.location for c in cameras}
        self.show_video = bool(cfg["dashboard"]["show_video"])
        self.video_width = int(cfg["dashboard"]["video_width"])
        self._stop_event = threading.Event()
        self._pool = ThreadPoolExecutor(max_workers=4)
        self._last_prune = 0.0

        # person tracking: one tracker per camera; the person numbers continue after a restart
        t = cfg["tracking"]
        self.conf_low = min(float(t["conf_low"]), float(cfg["model"]["conf"]))
        self.trackers = {c.cam_id: PersonTracker(
            c.cam_id, int(db.get_state(f"track_seq_{c.cam_id}", 1) or 1), t["max_gap_seconds"], t["reid_seconds"],
            hold_seconds=t["hold_seconds"], exit_hold_seconds=t["exit_hold_seconds"],
            edge_hold_seconds=t["edge_hold_seconds"], min_hits=t["min_hits"], conf_new=cfg["model"]["conf"],
            confirm_conf=t["confirm_conf"], walk_speed=t["walk_speed"])
            for c in cameras}
        self.debug = bool(cfg["analytics"].get("debug_log"))
        self.cam_zone = zone_of_camera(cfg)
        self.inside_zones = {z["key"] for z in cfg["zones"] if z["inside"]}
        self.zone_people = {}       # minute -> zone -> [(global id, activity, speed)] in the areas
        self.zone_done = ""         # minutes before this are written
        self.frame_seq = 0
        self.occ_logged = None      # inside occupancy of the last occupancy event
        self.occ_since = {}         # candidate state -> since when it holds

        from .detector import make_detector
        m = cfg["model"]
        # Ultralytics .pt on the PC (CPU / NVIDIA GPU) or a .hef on the AI HAT+ 2 (Raspberry Pi 5)
        self.detector = make_detector(m)
        self.device = m["device"]
        self.accelerator = self.detector.accelerator
        self.on_gpu = self.accelerator != "CPU"
        self.model_name = self.detector.model_name
        self.batch = int(m["batch"]) or self.detector.default_batch(len(cameras))
        self.imgsz, self.conf = int(self.detector.imgsz), float(m["conf"])
        self.classes, self.class_note = person_class_ids(self.detector.names, m["classes"])
        self.crop_to_area = cfg["setup"]["detect_in"] == "area"
        self.crop_per_area = bool(cfg["setup"]["crop_per_area"])
        self._idle_jpeg_at = {}
        state.update_info(model=self.model_name, imgsz=self.imgsz, conf=self.conf,
                          device=self.accelerator, tick_seconds=self.tick,
                          classes=self.class_note, detect_in=cfg["setup"]["detect_in"])

    def stop(self):
        self._stop_event.set()

    # ------------------------------------------------------------------
    def _thumbnail(self, frame):
        """Plain picture for the dashboard. Boxes, IDs and detection areas are drawn by the
        browser over it (they can be switched on and off there)."""
        scale = min(1.0, self.video_width / frame.shape[1])
        img = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) \
            if scale < 1.0 else frame
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return buf.tobytes() if ok else None

    def _crops(self, cam_id, frame):
        """[(x offset, y offset, picture YOLO looks at)]: the parts around the detection
        areas (one per area, or the whole picture)."""
        if not (self.crop_to_area and self.roi):
            return [(0, 0, frame)]
        h, w = frame.shape[:2]
        rects = self.roi.crops(cam_id, w, h, self.crop_per_area)
        if not rects:
            return [(0, 0, frame)]
        return [(x1, y1, frame[y1:y2, x1:x2]) for x1, y1, x2, y2 in rects]

    def _idle(self, cam, s):
        """Camera not analysed (waiting for setup, or set up as not analysed): picture only."""
        mode = self.roi.mode(cam.cam_id) if self.roi else "full"
        fh, fw = s.frame.shape[:2]
        self.state.update_camera(cam.cam_id, setup=mode, count=None, detected=None, moving=None, total=None,
                                 index=None, people=[], frame_size=[fw, fh], last_ts=None, crop=None)
        if not self.show_video or time.time() - self._idle_jpeg_at.get(cam.cam_id, 0) < 2.0:
            return
        jpeg = self._thumbnail(s.frame)
        if jpeg:
            self._idle_jpeg_at[cam.cam_id] = time.time()
            self.frame_seq += 1
            self.state.update_camera(cam.cam_id, frame_seq=self.frame_seq)
            self.state.set_jpeg(cam.cam_id, jpeg, self.frame_seq)

    def _process(self, batch):
        """batch: list of (camera, sample). Returns (second_rows, minute_rows, person_rows)."""
        crops = [self._crops(cam.cam_id, s.frame) for cam, s in batch]
        flat = [c for cs in crops for c in cs]
        results = []
        step = max(self.batch, 1)
        for i in range(0, len(flat), step):
            results += self.detector.predict([c[2] for c in flat[i:i + step]], self.conf_low, self.classes)
        # boxes back in whole-picture pixels, per camera; duplicates (same person in two
        # overlapping parts, or a part of a body) removed
        all_boxes, all_conf, k = [], [], 0
        for cs in crops:
            boxes, confs = [], []
            for ox, oy, _ in cs:
                r_boxes, r_confs = results[k]
                k += 1
                boxes += [(int(b[0]) + ox, int(b[1]) + oy, int(b[2]) + ox, int(b[3]) + oy) for b in r_boxes]
                confs += [float(c) for c in r_confs]
            keep = dedupe(boxes, confs)
            all_boxes.append([boxes[i] for i in keep])
            all_conf.append([confs[i] for i in keep])
        per_box = list(self._pool.map(
            lambda item: box_motions(item[0].prev_gray, item[0].gray, item[1], item[0].scale),
            [(s, boxes) for (_, s), boxes in zip(batch, all_boxes)]))

        sec_rows, min_rows, person_rows = [], [], []
        for (cam, s), boxes, confs, flows, cs in zip(batch, all_boxes, all_conf, per_box, crops):
            ts = datetime.fromtimestamp(s.ts)
            ts_text = ts.strftime("%Y-%m-%d %H:%M:%S")
            fh, fw = s.frame.shape[:2]
            crop = [[round(ox / fw, 4), round(oy / fh, 4), round((ox + sub.shape[1]) / fw, 4),
                     round((oy + sub.shape[0]) / fh, 4)] for ox, oy, sub in cs if sub is not s.frame] or None
            # tracking runs on every detection in the crops, so IDs survive stepping out of /
            # into the area; weak boxes that continue nobody are dropped here
            tracker = self.trackers[cam.cam_id]
            ids = tracker.update(boxes, s.frame, s.ts, confs)
            kept = [j for j, r in enumerate(ids) if r is not None]
            boxes, confs, flows, ids = ([x[j] for j in kept] for x in (boxes, confs, flows, ids))
            in_roi = self.roi.classify(cam.cam_id, boxes, fw, fh) if self.roi else [True] * len(boxes)
            flow_dt = (s.ts - s.prev_ts) if s.prev_ts else None
            persons = []
            for b, conf, flow, inside, (gid, seq, speed) in zip(boxes, confs, flows, in_roi, ids):
                activity, speed_f, moving = person_measure(flow, b, speed, flow_dt, self.settings)
                tracker.mark(gid, inside, self.roi.near_edge(cam.cam_id, b, fw, fh) if self.roi else False, speed_f)
                new = not tracker.confirmed(gid)          # not counted until confirmed
                inside = bool(inside and not new)
                persons.append({"gid": gid, "seq": seq, "conf": conf, "in_roi": inside, "flow": flow,
                                "activity": activity, "speed": speed_f, "moving": moving, "new": new, "box": b})
                raw_a = (flow / max(1.0, b[3] - b[1]) / flow_dt) if flow is not None and flow_dt else None
                person_rows.append((ts_text, cam.cam_id, gid, round(conf, 3), int(inside),
                                    None if flow is None else round(flow, 4),
                                    None if activity is None else round(activity, 5),
                                    None if speed_f is None else round(speed_f, 4), int(moving), *b,
                                    None if raw_a is None else round(raw_a, 5),
                                    None if speed is None else round(speed, 4)))
            inside_persons = [p for p in persons if p["in_roi"]]
            # persons sitting in the area that YOLO missed in this picture: still present
            held = [tr for tr in tracker.held if tr.inside]
            summary = sample_summary(inside_persons)
            summary["occupancy"] += len(held)
            summary["held"] = len(held)
            people = [(p["gid"], p["activity"], p["speed"]) for p in inside_persons]
            legacy = frame_motion([p["flow"] for p in inside_persons])   # the original frame score
            sec_rows.append((ts_text, cam.cam_id, summary["occupancy"], round(legacy, 6), len(boxes),
                             summary["moving"], None if summary["total"] is None else round(summary["total"], 6)))
            finished = self.windows[cam.cam_id].push(ts, summary, people, legacy, len(boxes))
            if finished:
                min_rows.append(self._minute_row(cam.cam_id, finished))
            minute = window_start_of(ts).strftime("%Y-%m-%d %H:%M:%S")
            if minute >= self.zone_done:                 # never reopen a minute already written
                zone = self.cam_zone.get(cam.cam_id)
                bucket = self.zone_people.setdefault(minute, {})
                bucket.setdefault(zone, []).extend(people)
                if zone in self.inside_zones:
                    bucket.setdefault("_inside", []).extend(people)
            roll = self.rolling[cam.cam_id].push(s.ts, summary["total"])
            if self.debug:
                log.info("cam=%s t=%s flow_dt=%s detected=%d tracks=%s | in_area=%d moving=%d total=%s "
                         "index=%s | rolling n=%d mean=%s var=%s | persons: %s",
                         cam.cam_id, ts_text, None if flow_dt is None else round(flow_dt, 2), len(boxes),
                         [p["seq"] for p in persons], summary["occupancy"], summary["moving"],
                         summary["total"], motion_index(summary["total"], self.settings.index_ref),
                         roll["n"], roll["mean"], roll["var"],
                         "; ".join(f"#{p['seq']}{'' if p['in_roi'] else '(out)'} flow={p['flow'] and round(p['flow'], 2)}px "
                                   f"a={p['activity'] and round(p['activity'], 4)} v={p['speed'] and round(p['speed'], 3)} "
                                   f"{'moving' if p['moving'] else 'still'}" for p in persons))
            self.frame_seq += 1
            self.state.update_camera(
                cam.cam_id, count=summary["occupancy"], detected=len(boxes), moving=summary["moving"],
                frame_seq=self.frame_seq, frame_size=[fw, fh], flow_dt=None if flow_dt is None else round(flow_dt, 3),
                total=None if summary["total"] is None else round(summary["total"], 4),
                index=motion_index(summary["total"], self.settings.index_ref),
                motion=round(legacy, 4), last_ts=s.ts, rolling=roll, crop=crop,
                setup=self.roi.mode(cam.cam_id) if self.roi else "full",
                held=len(held),
                people=[{"id": p["seq"], "conf": round(p["conf"], 2), "in_roi": p["in_roi"], "moving": p["moving"],
                         "new": p["new"], "held": False,
                         "activity": None if p["activity"] is None else round(p["activity"], 4),
                         "speed": None if p["speed"] is None else round(p["speed"], 3),
                         "box": [round(b[0] / fw, 4), round(b[1] / fh, 4), round(b[2] / fw, 4), round(b[3] / fh, 4)]}
                        for p, b in zip(persons, boxes)]
                + [{"id": tr.seq, "conf": None, "in_roi": True, "moving": False, "new": False, "held": True,
                    "activity": None, "speed": None, "since": round(s.ts - tr.t, 1),
                    "box": [round(tr.box[0] / fw, 4), round(tr.box[1] / fh, 4), round(tr.box[2] / fw, 4), round(tr.box[3] / fh, 4)]}
                   for tr in held])
            if self.show_video:
                jpeg = self._thumbnail(s.frame)
                if jpeg:
                    self.state.set_jpeg(cam.cam_id, jpeg, self.frame_seq)
        return sec_rows, min_rows, person_rows

    def _occupancy_events(self):
        """Event log: the lab becomes occupied / empty, or occupancy changes a lot. A state
        must hold for 2 minutes before it is logged (no flicker from missed detections)."""
        cams, _, _ = self.state.snapshot()
        counts = [c["count"] for cid, c in cams.items() if self.cam_zone.get(cid) in self.inside_zones
                  and c.get("count") is not None and c.get("connected")]
        if not counts:
            return
        now, n = time.time(), sum(counts)
        moving = sum(c.get("moving") or 0 for cid, c in cams.items() if self.cam_zone.get(cid) in self.inside_zones)
        last = self.occ_logged
        if last is None:
            self.occ_logged = n
            return
        big = abs(n - last) >= max(3, 0.3 * max(last, 1))
        candidate = "empty" if n == 0 and last > 0 else "occupied" if n > 0 and last == 0 else "change" if big else None
        if candidate is None:
            self.occ_since = {}
            return
        since = self.occ_since.setdefault(candidate, now)
        if now - since < 120:
            return
        text = {"empty": "inside the lab: nobody detected any more",
                "occupied": f"inside the lab: occupied, {n} persons",
                "change": f"inside the lab: occupancy changed from {last} to {n} persons"}[candidate]
        self.db.write_event("occupancy", text + f" ({moving} moving)", details={"persons": n, "moving": moving, "before": last})
        self.occ_logged, self.occ_since = n, {}

    def _zone_rows(self, before):
        """Per-zone person statistics of the minutes that ended before `before`."""
        rows = []
        self.zone_done = max(self.zone_done, before)
        for minute in sorted(m for m in self.zone_people if m < before):
            for zone, people in self.zone_people.pop(minute).items():
                if zone is not None:
                    rows.append(dict(person_stats(people), window_start=minute, zone=zone))
        return rows

    def _minute_row(self, cam_id, row):
        row = dict(row, camera=cam_id, camera_name=self.names[cam_id],
                   location=self.locations[cam_id])
        self.state.update_camera(cam_id, minute=row)
        return row

    def _close_finished_windows(self, now):
        rows = []
        for cam_id, window in self.windows.items():
            finished = window.flush_if_over(now)
            if finished:
                rows.append(self._minute_row(cam_id, finished))
        return rows

    # ------------------------------------------------------------------
    def run(self):
        next_tick = time.monotonic()
        while not self._stop_event.is_set():
            delay = next_tick - time.monotonic()
            if delay > 0 and self._stop_event.wait(delay):
                break
            tick_start = time.monotonic()

            # Every sample handed out after this moment is newer than `cutoff`.
            cutoff = datetime.now() - timedelta(seconds=1)
            samples = []
            for c in self.cameras:
                s = c.take()
                if s is None:
                    continue
                if self.roi is None or self.roi.analysed(c.cam_id):
                    samples.append((c, s))
                else:
                    self._idle(c, s)
            sec_rows, min_rows, person_rows = [], [], []
            for i in range(0, len(samples), self.batch):
                if self._stop_event.is_set():
                    break
                sec, mins, persons = self._process(samples[i:i + self.batch])
                sec_rows += sec
                min_rows += mins
                person_rows += persons
            min_rows += self._close_finished_windows(cutoff)
            self.db.write_seconds(sec_rows)
            self.db.write_person_obs(person_rows)
            self.db.write_minutes(min_rows)
            # samples handed out later are newer than `cutoff`, so every minute before
            # cutoff's minute is complete: written together with the camera minutes
            done = window_start_of(cutoff).strftime("%Y-%m-%d %H:%M:%S")
            zone_rows = self._zone_rows(done)
            self.db.write_zone_person_minutes(zone_rows)
            if zone_rows:
                for cam_id, tracker in self.trackers.items():
                    self.db.set_state(f"track_seq_{cam_id}", tracker.next_seq)
            for row in min_rows:
                print(f"  [minute] {row['window_start']}  {row['camera_name']:<10} {row['location']:<18} "
                      f"persons={row['n_person']:>3} ids={row['unique_persons']:>3} "
                      f"moving={row['n_moving_mean']:>5.1f}  motion={_f(row['motion_total'], 4)} BH/s "
                      f"var={_f(row['motion_total_var'], 6)} index={_f(row['motion_index'], 1)}  "
                      f"per person mu={_f(row['mu_person'], 4)} var={_f(row['sigma2_person'], 6)}  "
                      f"pictures={row['n_samples']}")
                if self.debug:
                    log.info("minute %s", row)

            if samples:
                self._occupancy_events()
            took = time.monotonic() - tick_start
            if samples:
                self.state.update_info(round_seconds=round(took, 2), cameras_in_round=len(samples))
            if time.time() - self._last_prune > 3600:
                self.db.prune_seconds(self.cfg["pipeline"]["keep_seconds_days"])
                self._last_prune = time.time()
            # Behind schedule: resync to "now" instead of bursting to catch up.
            next_tick = time.monotonic() if took > self.tick else tick_start + self.tick

    def flush(self):
        """Write the partly filled windows on shutdown."""
        rows = []
        for cam_id, window in self.windows.items():
            finished = window.flush()
            if finished:
                rows.append(self._minute_row(cam_id, finished))
        self.db.write_minutes(rows)
        self.db.write_zone_person_minutes(self._zone_rows("9999"))
        for cam_id, tracker in self.trackers.items():
            self.db.set_state(f"track_seq_{cam_id}", tracker.next_seq)
        self._pool.shutdown(wait=False)
