"""Detection areas: polygon test, foot point rule, validation, storage."""
import json

import pytest

from iaqcam.roi import RoiStore, point_in_polygon, validate

SQUARE = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]]


def test_point_in_polygon():
    assert point_in_polygon(0.5, 0.5, SQUARE)
    assert not point_in_polygon(0.1, 0.5, SQUARE)
    l_shape = [[0, 0], [1, 0], [1, 0.5], [0.5, 0.5], [0.5, 1], [0, 1]]
    assert point_in_polygon(0.25, 0.75, l_shape) and not point_in_polygon(0.75, 0.75, l_shape)


def test_feet_decide(tmp_path):
    store = RoiStore(tmp_path / "roi.json")
    store.set(1, [{"points": SQUARE}])
    # box whose head is inside but feet below the area -> outside
    assert store.classify(1, [(900, 300, 1000, 1000)], 1920, 1080) == [False]
    # head above the area, feet inside -> inside
    assert store.classify(1, [(900, 50, 1000, 700)], 1920, 1080) == [True]


def test_setup_first_then_modes(tmp_path):
    store = RoiStore(tmp_path / "roi.json")
    # not set up: not analysed, nobody counts
    assert store.mode(2) == "setup" and not store.analysed(2)
    assert store.classify(2, [(0, 0, 10, 10)], 100, 100) == [False]
    assert store.summary([1, 2])["pending"] == [1, 2]
    store.set(2, [], mode="full")
    assert store.analysed(2) and store.classify(2, [(0, 0, 10, 10)], 100, 100) == [True]
    store.set(2, [{"points": SQUARE}], mode="area")
    assert store.classify(2, [(0, 0, 10, 10)], 100, 100) == [False]
    store.set(2, [{"points": SQUARE}], mode="skip")
    assert not store.analysed(2) and store.get(2)        # areas kept while off
    assert store.summary([1, 2]) == {"modes": {1: "setup", 2: "skip"}, "pending": [1], "analysed": [],
                                     "done": 1, "total": 2}
    store.reset(2)
    assert store.mode(2) == "setup"


def test_area_mode_needs_an_enabled_area(tmp_path):
    store = RoiStore(tmp_path / "roi.json")
    with pytest.raises(ValueError):
        store.set(1, [{"points": SQUARE, "enabled": False}], mode="area")
    with pytest.raises(ValueError):
        store.set(1, [], mode="area")
    assert store.mode(1) == "setup"                       # nothing saved


def test_setup_not_required_keeps_old_behaviour(tmp_path):
    store = RoiStore(tmp_path / "roi.json", require_setup=False)
    assert store.mode(5) == "full" and store.classify(5, [(0, 0, 10, 10)], 100, 100) == [True]


def test_v6_file_is_read(tmp_path):
    path = tmp_path / "roi.json"
    path.write_text(json.dumps({"1": [{"name": "a", "points": SQUARE}], "2": []}))
    store = RoiStore(path)
    assert store.mode(1) == "area" and store.mode(2) == "full" and store.mode(3) == "setup"


def test_crop_around_areas_with_head_room(tmp_path):
    store = RoiStore(tmp_path / "roi.json", crop_margin=0.05, head_room=0.3)
    store.set(1, [{"points": [[0.4, 0.5], [0.6, 0.5], [0.6, 0.7], [0.4, 0.7]]}])
    assert store.crop(1, 1000, 1000) == (350, 200, 650, 750)
    # a person standing at the back edge of the area: feet y=0.5, body 0.25 high -> inside the crop
    x1, y1, x2, y2 = store.crop(1, 1000, 1000)
    assert y1 <= 250
    store.set(2, [], mode="full")
    assert store.crop(2, 1000, 1000) is None
    store.set(3, [{"points": [[0, 0], [1, 0], [1, 1], [0, 1]]}])
    assert store.crop(3, 1000, 1000) is None             # nearly all of it: whole picture


def test_saved_setup_survives_restart(tmp_path):
    path = tmp_path / "roi.json"
    RoiStore(path).set(4, [{"points": SQUARE}], user="admin")
    again = RoiStore(path)
    st = again.setup(4)
    assert st["mode"] == "area" and st["by"] == "admin" and st["configured"]


def test_normalised_storage_survives_restart_and_resolution(tmp_path):
    path = tmp_path / "roi.json"
    RoiStore(path).set(3, [{"name": "bench", "points": SQUARE}])
    data = json.loads(path.read_text())
    assert data["3"]["areas"][0]["points"][0] == [0.2, 0.2]
    again = RoiStore(path)
    box_hd = (950, 400, 1050, 600)                 # feet at (1000, 600) of 1920x1080
    box_sd = tuple(v / 2 for v in box_hd)          # same place at 960x540
    assert again.classify(3, [box_hd], 1920, 1080) == again.classify(3, [box_sd], 960, 540) == [True]


@pytest.mark.parametrize("bad", [
    [{"points": [[0.1, 0.1], [0.2, 0.2]]}],                 # 2 points
    [{"points": [[0.1, 0.1], [1.2, 0.2], [0.3, 0.4]]}],     # outside the picture
    "not a list",
])
def test_validation_rejects(bad):
    with pytest.raises((ValueError, TypeError)):
        validate(bad)


def test_person_class_ids():
    from iaqcam.model_classes import person_class_ids
    coco = {0: "person", 1: "bicycle", 2: "car"}
    assert person_class_ids(coco, ["person"])[0] == [0]
    custom = {0: "helmet", 1: "Student", 2: "staff"}
    assert person_class_ids(custom, ["student", "staff"])[0] == [1, 2]
    assert person_class_ids(custom, [2])[0] == [2]
    assert person_class_ids({0: "worker"}, ["person"])[0] == [0]      # one-class model
    with pytest.raises(ValueError):
        person_class_ids(custom, ["person"])
