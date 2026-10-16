"""Fan owner: manual / AI modes, conflicts, failures, restart, and the Raspberry Pi agent
(run in DRY RUN / mock mode on this computer) over HTTP with its token and watchdog."""
import copy
import json
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from iaqcam.config import DEFAULTS
from iaqcam.db import Database
from iaqcam.fan_controller import FanError, MockFanController, RemoteFanController
from iaqcam.fan_service import FanService

APP = Path(__file__).resolve().parents[1]


def make_cfg(tmp_path, **ai):
    fan = copy.deepcopy(DEFAULTS["fan"])
    fan.update(enabled=True, dry_run=False, check_seconds=1)
    fan["ai"].update(pollutants={"pm10": {"start": 150, "stop": 100}}, activation_seconds=0,
                     recovery_seconds=0, min_on_seconds=0, min_off_seconds=0, stale_seconds=300, **ai)
    return {"fan": fan, "database": {"path": str(tmp_path / "t.db")}}


def sensor(db, value, ts):
    db.write_sensor_fields({"field3": "PM10"})
    n = db._con.execute("SELECT COUNT(*) FROM sensor_readings").fetchone()[0]
    db.write_sensor_entries([{"entry_id": n + 1, "created_at_utc": ts, "ts_local": ts, "window_start": ts[:16] + ":00",
                              **{f"field{i}": None for i in range(1, 9)}, "field3": value}])


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 9, 10, 0, 0)

    def __call__(self):
        return self.t


@pytest.fixture
def setup(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg["database"]["path"])
    clock = Clock()
    ctl = MockFanController()
    svc = FanService(cfg, db, ctl, clock)
    return cfg, db, clock, ctl, svc


def events(db, kind=None):
    rows = db._con.execute("SELECT kind, message, fan_state, ok FROM events ORDER BY id").fetchall()
    return [r for r in rows if kind is None or r[0] == kind]


def test_manual_on_off_and_ai_refused_buttons(setup):
    cfg, db, clock, ctl, svc = setup
    svc.step()
    assert svc.mode == "manual"
    ok, _ = svc.manual_command(True, "op")
    assert ok and ctl.signal is True and svc.status()["requested"] is True
    ok, _ = svc.manual_command(False, "op")
    assert ok and ctl.signal is False
    svc.set_mode("ai", "op")
    ok, msg = svc.manual_command(True, "op")
    assert not ok and "AI mode" in msg
    assert [e[0] for e in events(db)].count("fan_command") == 2


def test_ai_does_not_act_in_manual_mode(setup):
    cfg, db, clock, ctl, svc = setup
    sensor(db, 300, clock().strftime("%Y-%m-%d %H:%M:%S"))
    for _ in range(3):
        svc.step()
    assert ctl.signal is False                                     # manual mode, button not pressed
    assert any("start threshold" in e[1] for e in events(db, "threshold"))   # but the crossing is logged


def test_switch_to_ai_decides_at_once_and_back_keeps_state(setup):
    cfg, db, clock, ctl, svc = setup
    svc.step()
    sensor(db, 300, clock().strftime("%Y-%m-%d %H:%M:%S"))
    svc.set_mode("ai", "op")
    assert ctl.signal is True and "PM10" in svc.status()["decision"]["reason"]
    svc.set_mode("manual", "op")
    clock.t += timedelta(seconds=30)
    sensor(db, 20, clock().strftime("%Y-%m-%d %H:%M:%S"))
    svc.step()
    assert ctl.signal is True                                      # manual: AI no longer switches it off


def test_ai_recovery_and_stale_fail_safe(setup):
    cfg, db, clock, ctl, svc = setup
    svc.set_mode("ai")
    sensor(db, 300, clock().strftime("%Y-%m-%d %H:%M:%S"))
    svc.step()
    assert ctl.signal is True
    clock.t += timedelta(seconds=20)
    sensor(db, 120, clock().strftime("%Y-%m-%d %H:%M:%S"))         # between stop and start: keeps running
    svc.step()
    assert ctl.signal is True
    clock.t += timedelta(seconds=20)
    sensor(db, 50, clock().strftime("%Y-%m-%d %H:%M:%S"))
    svc.step(); clock.t += timedelta(seconds=1); svc.step()
    assert ctl.signal is False
    sensor(db, 300, clock().strftime("%Y-%m-%d %H:%M:%S"))
    svc.step()
    assert ctl.signal is True
    clock.t += timedelta(seconds=400)                              # no new reading: stale -> off
    svc.step()
    assert ctl.signal is False and "fail-safe" in svc.status()["decision"]["reason"]
    assert any(e[0] == "sensor" and "stale" in e[1] for e in events(db))


def test_failure_is_reported_and_retried(setup):
    cfg, db, clock, ctl, svc = setup
    svc.step()
    ctl.fail = True
    ok, msg = svc.manual_command(True, "op")
    assert not ok and "failed" in msg
    assert svc.status()["last_command"]["ok"] is False
    assert any(e[0] == "fan_error" for e in events(db))
    ctl.fail = False
    ok, _ = svc.manual_command(True, "op")
    assert ok and ctl.signal is True


def test_concurrent_button_press_refused(setup):
    cfg, db, clock, ctl, svc = setup
    svc.step()
    svc._lock.acquire()
    import threading
    result = {}
    t = threading.Thread(target=lambda: result.update(r=svc.manual_command(True, "op2")))
    t.start(); t.join()
    svc._lock.release()
    assert result["r"][0] is False and "still being carried out" in result["r"][1]


def test_restart_restores_mode_and_manual_state(tmp_path):
    cfg = make_cfg(tmp_path)
    db = Database(cfg["database"]["path"])
    svc = FanService(cfg, db, MockFanController(), Clock())
    svc.step()
    svc.manual_command(True)
    ctl2 = MockFanController()
    svc2 = FanService(cfg, db, ctl2, Clock())                        # app restarted
    svc2.step()
    assert svc2.mode == "manual" and ctl2.signal is True


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_pi_agent_token_commands_and_watchdog(tmp_path):
    port = free_port()
    token = "test-token-123"
    proc = subprocess.Popen([sys.executable, str(APP / "tests" / "fan_agent_mock" / "agent.py"), "--mock", "--port", str(port),
                             "--host", "127.0.0.1", "--watchdog-seconds", "3", "--on-comm-loss", "off",
                             "--token-file", str(tmp_path / "tok.txt"), "--log", str(tmp_path / "agent.log")],
                            env={"FAN_AGENT_TOKEN": token, "PATH": "/usr/bin:/bin"})
    try:
        url = f"http://127.0.0.1:{port}"
        for _ in range(50):
            try:
                urllib.request.urlopen(url + "/status", timeout=0.2)
            except urllib.error.HTTPError:
                break                                            # answers (401 without the token)
            except OSError:
                time.sleep(0.1)
        bad = RemoteFanController(url, "wrong", timeout=1, retries=0)
        with pytest.raises(FanError):
            bad.fan_on("x")
        ctl = RemoteFanController(url, token, timeout=1, retries=1)
        ctl.initialize()
        st = ctl.fan_on("test ON")
        assert st["requested"] is True and st["signal"] is True
        assert ctl.status()["agent"]["last_reason"].startswith("test ON")
        time.sleep(6)                                            # no contact > watchdog: comm-loss action OFF
        st = ctl.status()
        assert st["signal"] is False and "no contact" in st["agent"]["last_reason"]
    finally:
        proc.terminate()
        proc.wait(5)
    ctl = RemoteFanController(url, token, timeout=0.5, retries=0)
    with pytest.raises(FanError):                                # agent gone: clear error
        ctl.fan_off("x")
