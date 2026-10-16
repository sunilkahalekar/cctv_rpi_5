"""Occupancy, motion and variance: definitions and the validation scenarios."""
import math

from iaqcam.analytics import Settings, motion_index, person_measure, sample_summary, window_stats
from validate_motion import check, scenarios

S = Settings({"analytics": {"activity_noise": 0.005, "speed_noise": 0.05, "index_ref": 0.3}})


def test_activity_is_resolution_independent():
    # the same body movement: 30 px in a 300 px box, or 15 px in a 150 px box, over 0.12 s
    a1, _, _ = person_measure(30.0, (0, 0, 120, 300), None, 0.12, S)
    a2, _, _ = person_measure(15.0, (0, 0, 60, 150), None, 0.12, S)
    assert math.isclose(a1, a2) and math.isclose(a1, 30 / 300 / 0.12)


def test_noise_filter_and_moving():
    a, v, moving = person_measure(0.1, (0, 0, 100, 300), 0.01, 0.12, S)    # jitter only
    assert a == 0.0 and v == 0.0 and not moving
    a, v, moving = person_measure(0.1, (0, 0, 100, 300), 0.4, 0.12, S)     # walks, flow small
    assert moving and v == 0.4
    a, v, moving = person_measure(None, (0, 0, 100, 300), None, None, S)   # no flow, first picture
    assert a is None and v is None and not moving


def test_empty_and_unknown_are_not_zero_variance():
    assert sample_summary([]) == {"occupancy": 0, "moving": 0, "total": 0.0}
    assert sample_summary([{"activity": None, "moving": False}])["total"] is None
    one = window_stats([sample_summary([{"activity": 0.2, "moving": True}])], [(1, 0.2, None)], [5.0], 0.3)
    assert one["motion_total"] == 0.2 and one["motion_total_var"] is None and one["sigma2_person"] is None


def test_temporal_variance_is_population_variance():
    samples = [sample_summary([{"activity": x, "moving": x > 0}]) for x in (0.0, 0.2, 0.4)]
    row = window_stats(samples, [(1, x, None) for x in (0.0, 0.2, 0.4)], [0, 0, 0], 0.3)
    assert math.isclose(row["motion_total_var"], ((0.2) ** 2 * 2) / 3, rel_tol=1e-6)
    assert row["n_moving_mean"] == round(2 / 3, 4)


def test_more_people_do_not_shrink_the_motion():
    walk = [{"activity": 0.5, "moving": True}]
    sit = [{"activity": 0.0, "moving": False}] * 20
    assert sample_summary(walk + sit)["total"] == sample_summary(walk)["total"]
    assert sample_summary(walk * 3)["total"] == 3 * sample_summary(walk)["total"]


def test_index_scale():
    assert motion_index(0, 0.3) == 0 and motion_index(None, 0.3) is None
    assert motion_index(0.3, 0.3) == 63.2 and motion_index(0.9, 0.3) == 95.0


def test_validation_scenarios():
    problems = check(scenarios())
    assert not problems, problems
