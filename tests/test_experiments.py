"""Experiment log on the Events page: start / note / stop, stored as events and exported for the paper."""
import json
import sys
from pathlib import Path

from iaqcam.config import load_config
from iaqcam.db import Database
from iaqcam.web import create_app


class State:
    def __getattr__(self, name):
        raise AttributeError(name)


def test_experiment_log(tmp_path):
    cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.yaml"))
    cfg["database"]["path"] = str(tmp_path / "t.db")
    cfg["dashboard"].update(require_login=False, users_file=str(tmp_path / "u.json"),
                            secret_file=str(tmp_path / "s.key"))
    db = Database(tmp_path / "t.db")
    app = create_app(cfg, State(), db=db)
    c = app.test_client()
    env = {"REMOTE_ADDR": "127.0.0.1"}
    r = c.post("/api/experiments", json={"action": "start", "type": "decay_off", "note": "door closed"}, environ_base=env)
    assert r.status_code == 200 and r.json["active"]["type"] == "decay_off"
    assert c.post("/api/experiments", json={"action": "start", "type": "fan_step"}, environ_base=env).status_code == 409
    assert c.post("/api/experiments", json={"action": "note", "note": "6 people left at 18:02"}, environ_base=env).json["ok"]
    assert c.post("/api/experiments", json={"action": "stop"}, environ_base=env).json["active"] is None
    # someone else on the network may read but not write
    assert c.post("/api/experiments", json={"action": "start", "type": "other"},
                  environ_base={"REMOTE_ADDR": "10.0.0.9"}).status_code == 403
    g = c.get("/api/experiments", environ_base=env).json
    assert g["active"] is None and len(g["events"]) == 3
    # the research export reads them back
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
    import sqlite3
    con = sqlite3.connect(tmp_path / "t.db")
    stops = [json.loads(d) for (d,) in con.execute("SELECT details FROM events WHERE kind = 'experiment'")
             if json.loads(d)["action"] == "stop"]
    assert stops[0]["type"] == "decay_off" and stops[0]["note"] == "door closed" and stops[0]["end"]
