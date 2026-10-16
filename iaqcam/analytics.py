"""
Occupancy and motion analytics (version 6).

Definitions (all per camera, only persons whose feet are inside the camera's detection
area / ROI; see roi.py):

Per person, per analysed picture
  activity  a = mean optical-flow magnitude inside the person's box, divided by the box
              height and by the flow interval  ->  body heights per second (BH/s).
              Dividing by the box height makes a person far from the camera and one close
              to it, and cameras of different resolution, comparable. Values below
              `activity_noise` are compression / detection jitter and are set to 0.
  speed     v = distance the feet moved since the person's previous picture (tracking),
              divided by body height and elapsed time  ->  BH/s. Below `speed_noise` = 0.
              Unknown (None) for the first picture of a person.
  moving    a >= activity_noise  or  v >= speed_noise.

Per picture (sample)
  occupancy N = persons inside the ROI (one tracked ID per box, never counted twice)
  moving    K = persons inside the ROI that move
  total motion M = sum of a over the persons inside the ROI (BH/s). It grows with the
              number of moving people and with how much each one moves. 0 for an empty
              area; unknown when the picture had no previous frame for optical flow.

Per minute (window)
  occupancy: maximum, mean and the typical count (most frequent N, ties -> the larger)
             of the minute; with one picture per second that is the count over 60 pictures
             (persons held present are included, see tracking.py); moving: mean and max of K
  motion_total      = mean of M over the window's pictures
  motion_total_var  = temporal variance of M: mean of squared deviations of M from its
                      mean (population variance, as in iaq-edge-pipeline). Needs at least
                      2 pictures with M; otherwise NULL (not 0).
  motion_index      = 100 * (1 - exp(-motion_total / index_ref)): 0 = nobody moves,
                      63 = index_ref, 95 = three times index_ref. Display scale only.
  mu_person / sigma2_person = mean / variance of a over all person observations of the
                      window (differences between people and over time). NULL below 2.
  mu_speed          = mean of the measured v.
  mu_motion / sigma2_motion  the original H_t of iaq-edge-pipeline (frame score = mean
                      flow in pixels over the boxes), kept for comparison.

Why the original variance stayed low with many people: the frame score is the mean over
all boxes, so with N people its picture-to-picture variation shrinks roughly like 1/N;
with 2-3 pictures per minute the variance had almost no samples; zone values were
averages over cameras including empty ones; and flow in pixels made far-away people
count less than near ones.
"""
import math

import numpy as np


class Settings:
    def __init__(self, cfg=None):
        a = (cfg or {}).get("analytics", {}) if isinstance(cfg, dict) else {}
        self.activity_noise = float(a.get("activity_noise", 0.005))
        self.speed_noise = float(a.get("speed_noise", 0.05))
        self.index_ref = max(1e-6, float(a.get("index_ref", 0.3)))
        self.min_flow_dt = float(a.get("min_flow_dt", 0.03))
        self.max_flow_dt = float(a.get("max_flow_dt", 5.0))


def motion_index(total, ref):
    """0-100 display scale of the total motion (None stays None)."""
    if total is None:
        return None
    return round(100.0 * (1.0 - math.exp(-max(0.0, float(total)) / ref)), 1)


def person_measure(flow_px, box, speed, flow_dt, s):
    """(activity, speed, moving) of one person. flow_px: mean flow in the box (full-res
    pixels over the flow interval) or None; speed: BH/s from the tracker or None."""
    height = max(1.0, float(box[3]) - float(box[1]))
    activity = None
    if flow_px is not None and flow_dt and s.min_flow_dt <= flow_dt <= s.max_flow_dt:
        activity = flow_px / height / flow_dt
        if activity < s.activity_noise:
            activity = 0.0
    if speed is not None and speed < s.speed_noise:
        speed = 0.0
    moving = bool((activity is not None and activity > 0.0) or (speed is not None and speed > 0.0))
    return activity, speed, moving


def sample_summary(persons):
    """persons: dicts with activity / moving for the persons inside the ROI of one picture."""
    known = [p["activity"] for p in persons if p["activity"] is not None]
    if persons and not known:
        total = None                       # no optical flow for this picture
    else:
        total = float(sum(known))          # empty area: 0
    return {"occupancy": len(persons), "moving": sum(1 for p in persons if p["moving"]),
            "total": total}


def _var(values):
    return round(float(np.var(values)), 8) if len(values) >= 2 else None


def _mean(values, digits=6):
    return round(float(np.mean(values)), digits) if len(values) else None


def person_stats(people):
    """people: (global id, activity, speed) per person observation inside the ROI."""
    acts = [a for _, a, _ in people if a is not None]
    speeds = [v for _, _, v in people if v is not None]
    return {"unique_persons": len({g for g, _, _ in people}), "n_obs": len(people),
            "mu_person": _mean(acts),          # None without any person: undefined, not 0
            "sigma2_person": _var(acts), "mu_speed": _mean(speeds)}


def window_stats(samples, people, legacy, index_ref):
    """samples: sample_summary dicts of one camera's minute; people: person observations;
    legacy: original frame scores (pixels). Returns the minute's columns."""
    occ = [s["occupancy"] for s in samples]
    mov = [s["moving"] for s in samples]
    totals = [s["total"] for s in samples if s["total"] is not None]
    mu_total = _mean(totals)
    legacy_arr = np.array(legacy, dtype=float)
    row = {
        "n_person": max(occ) if occ else 0,
        "n_person_mean": round(float(np.mean(occ)), 4) if occ else 0.0,
        "n_person_mode": max(set(occ), key=lambda v: (occ.count(v), v)) if occ else 0,
        "n_moving_mean": round(float(np.mean(mov)), 4) if mov else 0.0,
        "n_moving_max": max(mov) if mov else 0,
        "motion_total": mu_total,
        "motion_total_var": _var(totals),
        "motion_index": motion_index(mu_total, index_ref),
        "n_valid": len(totals),
        "mu_motion": round(float(legacy_arr.mean()), 6) if legacy_arr.size else 0.0,
        "sigma2_motion": round(float(legacy_arr.var()), 6) if legacy_arr.size else 0.0,
    }
    row.update(person_stats(people))
    return row


class Rolling:
    """Last `seconds` of one camera's samples, for the live display (not stored)."""

    def __init__(self, seconds=60.0):
        self.seconds = float(seconds)
        self.items = []                    # (ts, total)

    def push(self, ts, total):
        self.items.append((ts, total))
        self.items = [(t, v) for t, v in self.items if ts - t <= self.seconds]
        vals = [v for _, v in self.items if v is not None]
        return {"n": len(vals), "mean": _mean(vals, 4), "var": _var(vals)}
