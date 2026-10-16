"""
Reproducible validation of occupancy, motion and variance (no camera, no YOLO needed).

    python tests/validate_motion.py

Runs the scenarios of the requirement on the synthetic camera (tests/sim.py) through the
real optical flow, tracker, detection areas and analytics, prints one line per scenario
and checks the expected behaviour. Exit code 1 if a check fails.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sim import Person, run_camera  # noqa: E402

WALK = 150.0          # px/s for a 300 px tall person = 0.5 body heights per second


def row_of(persons, **kw):
    return run_camera(persons, **kw)


def scenarios():
    out = {}
    out["1 empty lab"] = row_of([])
    out["2 one person still"] = row_of([Person(300, 500, seed=1)])
    out["3 five persons still"] = row_of([Person(150 + 300 * i, 500, seed=i + 1) for i in range(5)])
    out["4 one person walking"] = row_of([Person(100, 500, vx=WALK, seed=1)])
    out["5 four persons walking"] = row_of([Person(100, 200 + 120 * i, h=250, vx=WALK * (0.8 + 0.1 * i), seed=i + 1)
                                            for i in range(4)])
    # eight sitting in two rows, two walking in their own lane in front
    many = [Person(60 + 220 * (i % 4) + 900 * (i // 4 // 2), 60 + 330 * ((i // 4) % 2), h=260, seed=i + 1)
            for i in range(8)]
    many += [Person(100, 800, h=260, vx=WALK, seed=31), Person(1500, 800, h=260, vx=-WALK * 0.8, seed=32)]
    out["6 ten present, two walking"] = row_of(many)
    # 7: walks into the area (right half) and out again
    roi_right = [[[0.5, 0.0], [1.0, 0.0], [1.0, 1.0], [0.5, 1.0]]]
    out["7 enters and leaves area"] = row_of([Person(500, 500, vx=40, seed=3), Person(1300, 500, seed=4)],
                                             seconds=120, roi_polys=roi_right)
    # 8: hidden for 20 s in the middle (occlusion)
    out["8 occlusion, ID kept"] = row_of([Person(200, 500, vx=WALK / 3, seed=5, visible=lambda t: not 25 <= t < 45),
                                          Person(1200, 500, seed=6)], seconds=90)
    # 9: one walking person outside the area, one still person inside
    roi_left = [[[0.0, 0.0], [0.5, 0.0], [0.5, 1.0], [0.0, 1.0]]]
    out["9 walker outside area"] = row_of([Person(1300, 100, vy=WALK, seed=7), Person(300, 500, seed=8)],
                                          roi_polys=roi_left)
    # 10: same scene, two resolutions
    scene = lambda: [Person(100, 500, vx=WALK, seed=9), Person(900, 450, seed=10, wiggle=12)]  # noqa: E731
    out["10a scene at 1920x1080"] = row_of(scene())
    out["10b same at 960x540"] = row_of(scene(), size=(960, 540))
    # variance: constant walking vs. walking that starts and stops
    out["V1 two walking all the time"] = row_of([Person(100, 300, vx=WALK, seed=11), Person(100, 650, vx=WALK, seed=12)])
    stop_go = [Person(100, 300, seed=11), Person(100, 650, seed=12)]
    for p in stop_go:
        p.vx = 0.0
    out["V2 two walking only half the time"] = _stop_and_go(stop_go)
    out["V3 twelve still, one wiggling arms"] = row_of(
        [Person(60 + 150 * (i % 12), 400, h=240, seed=20 + i, wiggle=15 if i == 0 else 0) for i in range(12)])
    return out


def _stop_and_go(persons):
    """Persons walk during 0-15 s and 30-45 s, stand still otherwise."""
    class StopGo(Person):
        def box(self, t, sx=1.0, sy=1.0):
            walked = sum(max(0.0, min(t, b) - a) for a, b in ((0, 15), (30, 45)))
            x = self._bounce(self.x + WALK * walked, 0.0, 1920.0 - self.w)
            return (x * sx, self.y * sy, (x + self.w) * sx, (self.y + self.h) * sy)
    return row_of([StopGo(p.x, p.y, p.h, seed=k) for k, p in enumerate(persons)])


def check(results):
    r = {k: v[0] for k, v in results.items()}
    ids = {k: v[2] for k, v in results.items()}
    problems = []

    def expect(cond, text):
        if not cond:
            problems.append(text)

    e, s1, s5 = r["1 empty lab"], r["2 one person still"], r["3 five persons still"]
    expect(e["n_person"] == 0 and e["motion_total"] == 0 and e["motion_total_var"] == 0 and e["mu_person"] is None
           and e["sigma2_person"] is None, "empty: occupancy 0, motion 0, per-person values undefined")
    expect(s1["n_person"] == 1 and s1["n_moving_mean"] == 0 and s1["motion_total"] < 0.003, "one still person: not moving")
    expect(s5["n_person"] == 5 and s5["n_moving_mean"] == 0, "five still persons: occupancy 5, nobody moving")
    w1, w4 = r["4 one person walking"], r["5 four persons walking"]
    expect(w1["n_moving_mean"] >= 0.9 and w1["motion_total"] > 0.05, "one walker: moving and motion > 0")
    expect(w4["n_moving_mean"] >= 3.6 and w4["motion_total"] > 2.5 * w1["motion_total"], "four walkers: about 4x motion")
    m = r["6 ten present, two walking"]
    expect(m["n_person"] == 10 and 1.6 <= m["n_moving_mean"] <= 2.4, "ten present, two moving counted")
    expect(m["motion_total"] < w4["motion_total"], "ten mostly still persons move less than four walkers")
    a = r["7 enters and leaves area"]
    expect(a["n_person"] == 2 and a["n_person_mean"] < 2 and a["unique_persons"] == 2,
           "enter/leave: occupancy varies, no extra IDs")
    expect(all(len(v) == 1 for v in ids["7 enters and leaves area"].values()), "enter/leave: each person one ID")
    expect(all(len(v) == 1 for v in ids["8 occlusion, ID kept"].values()), "occlusion: ID recovered")
    o = r["9 walker outside area"]
    expect(o["n_person"] == 1 and o["n_moving_mean"] == 0 and o["motion_total"] < 0.003,
           "outside walker ignored (occupancy 1, no motion)")
    hi, lo = r["10a scene at 1920x1080"], r["10b same at 960x540"]
    expect(abs(hi["motion_total"] - lo["motion_total"]) <= 0.2 * hi["motion_total"],
           "resolution: total motion within 20 % at half resolution")
    c, sg = r["V1 two walking all the time"], r["V2 two walking only half the time"]
    expect(sg["motion_total_var"] > 3 * c["motion_total_var"], "stop-and-go has a much higher temporal variance")
    expect(r["V3 twelve still, one wiggling arms"]["n_moving_mean"] >= 0.8, "arm movement counts as moving")
    return problems


def table(results):
    cols = ("n_person", "n_person_mean", "unique_persons", "n_moving_mean", "motion_total",
            "motion_total_var", "motion_index", "mu_person", "sigma2_person", "mu_speed", "n_samples")
    head = f"{'scenario':<36}" + "".join(f"{c[:12]:>13}" for c in cols)
    lines = [head, "-" * len(head)]
    for k, (row, _, _) in results.items():
        cells = []
        for c in cols:
            v = row.get(c)
            cells.append(f"{'NULL':>13}" if v is None else f"{v:>13.5g}" if isinstance(v, float) else f"{v:>13}")
        lines.append(f"{k:<36}" + "".join(cells))
    return "\n".join(lines)


if __name__ == "__main__":
    res = scenarios()
    print(table(res))
    print("\nmotion_total and mu_person in body heights per second; variances in (BH/s)^2; "
          "motion_index 0-100 with the default index_ref.")
    bad = check(res)
    print("\nAll checks passed." if not bad else "\nFAILED:\n  " + "\n  ".join(bad))
    sys.exit(1 if bad else 0)
