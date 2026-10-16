"""Raspberry Pi + ESP32 as alternative fan controllers: one active, standby kept OFF,
automatic switch-over when the active one stops answering, and dashboard selection."""
import pytest

from iaqcam.fan_controller import DualFanController, FanError


class FakeAgent:
    def __init__(self, name):
        self.url, self.label = f"http://{name}:8095", name
        self.up, self.requested, self.last_error, self.calls = True, False, None, []

    def _check(self):
        if not self.up:
            self.last_error = "not reachable"
            raise FanError(self.last_error)
        self.last_error = None

    def initialize(self):
        self._check()

    def _set(self, on, reason):
        self._check()
        self.requested = on
        self.calls.append(("on" if on else "off", reason))
        return {"ok": True, "requested": on, "signal": on}

    def fan_off(self, reason=""):
        return self._set(False, reason)

    def status(self):
        try:
            self._check()
        except FanError as exc:
            return {"ok": False, "error": str(exc), "requested": None}
        return {"ok": True, "requested": self.requested, "signal": self.requested}

    probe = status

    def cleanup(self):
        pass


def make(failover="auto"):
    pi, esp = FakeAgent("pi"), FakeAgent("esp32")
    notes = []
    d = DualFanController({"pi": pi, "esp32": esp}, "pi", failover, notes.append)
    return d, pi, esp, notes


def test_only_active_switches_and_standby_kept_off():
    d, pi, esp, _ = make()
    esp.requested = True                      # standby left ON from earlier
    d.initialize()
    assert esp.requested is False             # standby forced OFF
    d.fan_on("test")
    assert pi.requested is True and esp.requested is False
    st = d.status()
    assert st["active"] == "pi" and st["controllers"]["esp32"]["ok"] and not st["controllers"]["esp32"]["active"]


def test_failover_on_command_repeats_state_on_esp32():
    d, pi, esp, notes = make()
    d.initialize()
    pi.up = False
    d.fan_on("AI: PM2.5 high")
    assert d.active == "esp32" and esp.requested is True
    assert notes and "moved from pi to esp32" in notes[0]


def test_failover_on_heartbeat_keeps_requested_state():
    d, pi, esp, notes = make()
    d.initialize()
    d.fan_on("x")
    pi.up = False
    st = d.status()
    assert st["active"] == "esp32" and esp.requested is True and st["ok"]


def test_manual_failover_does_not_switch():
    d, pi, esp, _ = make("manual")
    d.initialize()
    pi.up = False
    with pytest.raises(FanError):
        d.fan_on("x")
    assert d.active == "pi" and esp.requested is False


def test_select_from_dashboard_moves_state_and_turns_old_off():
    d, pi, esp, _ = make()
    d.initialize()
    d.fan_on("x")
    d.select("esp32")
    assert d.active == "esp32" and esp.requested is True and pi.requested is False
    esp.up = False
    with pytest.raises(FanError):
        d.select("esp32") if False else d.agents["esp32"].initialize()


def test_both_down_raises():
    d, pi, esp, _ = make()
    pi.up = esp.up = False
    with pytest.raises(FanError):
        d.initialize()
