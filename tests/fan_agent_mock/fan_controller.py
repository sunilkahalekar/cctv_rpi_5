"""
Exhaust-fan controllers: one interface, three implementations.

    MockFanController    no hardware; for tests and for running without a Pi
    GpioFanController    drives the relay pin of the Raspberry Pi itself (RPi.GPIO, BCM)
    RemoteFanController  talks to pi_fan_agent.py on the Raspberry Pi over the LAN

This file only uses the Python standard library (RPi.GPIO on the Pi), so the same file is
copied to the Raspberry Pi next to pi_fan_agent.py.

What "confirmed" means here: `signal` is the level read back from the GPIO output pin. It
proves the control signal, not that the fan turns: that needs a feedback input (an
auxiliary contact or current sensor on `feedback_pin`). Without one, `running` is None.
"""
import json
import logging
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("iaqcam.fan")


class FanError(Exception):
    """The command could not be carried out (no hardware, no connection, refused)."""


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class FanController:
    kind = "base"

    def __init__(self):
        self._lock = threading.Lock()
        self.requested = None          # True / False / None (unknown)
        self.last_error = None
        self.updated = None

    def initialize(self):
        raise NotImplementedError

    def fan_on(self, reason=""):
        return self._set(True, reason)

    def fan_off(self, reason=""):
        return self._set(False, reason)

    def get_requested_state(self):
        return self.requested

    def status(self):
        """dict: requested, signal (pin level as on/off or None), running (feedback or None),
        ok, error, updated."""
        raise NotImplementedError

    def cleanup(self):
        pass

    def _set(self, on, reason):
        raise NotImplementedError


class MockFanController(FanController):
    """In-memory relay. `fail = True` makes every command fail (for tests)."""
    kind = "mock"

    def __init__(self):
        super().__init__()
        self.fail = False
        self.signal = None
        self.commands = []

    def initialize(self):
        self.signal = False
        self.requested = False
        self.updated = _now()

    def _set(self, on, reason):
        with self._lock:
            if self.fail:
                self.last_error = "mock controller set to fail"
                raise FanError(self.last_error)
            if self.requested != on:
                log.info("fan %s (mock) - %s", "ON" if on else "OFF", reason)
            self.requested = self.signal = on
            self.commands.append((on, reason))
            self.last_error, self.updated = None, _now()
            return self.status_unlocked()

    def status_unlocked(self):
        return {"requested": self.requested, "signal": self.signal, "running": None,
                "ok": not self.fail, "error": self.last_error, "updated": self.updated,
                "controller": "mock (no hardware)"}

    def status(self):
        with self._lock:
            return self.status_unlocked()


class GpioFanController(FanController):
    """Relay on a Raspberry Pi GPIO pin (BCM numbering; default GPIO 9 = header pin 21).

    active_high=True: HIGH switches the relay on (as in the supplied relay test). For an
    active-LOW relay module set active_high=False; nothing else changes.
    """
    kind = "gpio"

    def __init__(self, pin=9, active_high=True, initial_on=False, feedback_pin=None,
                 feedback_active_high=True):
        super().__init__()
        self.pin = int(pin)
        self.active_high = bool(active_high)
        self.initial_on = bool(initial_on)
        self.feedback_pin = None if feedback_pin in (None, "") else int(feedback_pin)
        self.feedback_active_high = bool(feedback_active_high)
        self.GPIO = None

    def _level(self, on):
        return self.GPIO.HIGH if on == self.active_high else self.GPIO.LOW

    def initialize(self):
        if self.GPIO is not None:
            return                                  # only once
        try:
            import RPi.GPIO as GPIO
        except (ImportError, RuntimeError) as exc:
            self.last_error = f"RPi.GPIO not available: {exc}"
            raise FanError(self.last_error)
        try:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            self.GPIO = GPIO
            GPIO.setup(self.pin, GPIO.OUT, initial=self._level(self.initial_on))
            if self.feedback_pin is not None:
                GPIO.setup(self.feedback_pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN
                           if self.feedback_active_high else GPIO.PUD_UP)
        except (RuntimeError, PermissionError, ValueError) as exc:
            self.GPIO = None
            self.last_error = f"GPIO setup failed (pin {self.pin}): {exc}"
            raise FanError(self.last_error)
        self.requested = self.initial_on
        self.updated = _now()
        log.info("GPIO %d ready, relay %s at start (active-%s)", self.pin,
                 "ON" if self.initial_on else "OFF", "HIGH" if self.active_high else "LOW")

    def _set(self, on, reason):
        with self._lock:
            if self.GPIO is None:
                raise FanError(self.last_error or "GPIO not initialised")
            try:
                self.GPIO.output(self.pin, self._level(on))
            except (RuntimeError, ValueError) as exc:
                self.last_error = f"GPIO write failed: {exc}"
                raise FanError(self.last_error)
            if self.requested != on:
                log.info("fan %s: GPIO %d %s - %s", "ON" if on else "OFF", self.pin,
                         "HIGH" if self._level(on) == self.GPIO.HIGH else "LOW", reason)
            self.requested, self.last_error, self.updated = on, None, _now()
            return self._status()

    def _status(self):
        signal = running = None
        if self.GPIO is not None:
            try:
                level = self.GPIO.input(self.pin)          # read-back of the output latch
                signal = (level == self.GPIO.HIGH) == self.active_high
                if self.feedback_pin is not None:
                    running = (self.GPIO.input(self.feedback_pin) == self.GPIO.HIGH) == self.feedback_active_high
            except (RuntimeError, ValueError) as exc:
                self.last_error = f"GPIO read failed: {exc}"
        return {"requested": self.requested, "signal": signal, "running": running,
                "ok": self.GPIO is not None and self.last_error is None, "error": self.last_error,
                "updated": self.updated, "controller": f"GPIO {self.pin} on this computer"}

    def status(self):
        with self._lock:
            return self._status()

    def cleanup(self):
        with self._lock:
            if self.GPIO is not None:
                try:
                    self.GPIO.output(self.pin, self._level(False))
                    self.GPIO.cleanup([self.pin] + ([self.feedback_pin] if self.feedback_pin is not None else []))
                except RuntimeError:
                    pass
                log.info("GPIO %d released (relay OFF)", self.pin)
                self.GPIO = None


class RemoteFanController(FanController):
    """pi_fan_agent.py on the Raspberry Pi. Commands are 'set state' (not 'toggle'), so a
    retry after a lost answer cannot switch the fan the wrong way."""
    kind = "remote"

    def __init__(self, url, token, timeout=3.0, retries=2):
        super().__init__()
        self.url = url.rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.last = {}

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        error = None
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(self.url + path, data=data, method=method, headers={
                "Authorization": "Bearer " + self.token, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return json.loads(resp.read().decode() or "{}")
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:200]
                raise FanError(f"fan agent refused ({exc.code}): {detail}")   # no retry
            except (urllib.error.URLError, OSError, ValueError) as exc:
                error = exc
                time.sleep(0.3 * (attempt + 1))
        raise FanError(f"fan agent at {self.url} not reachable: {error}")

    def initialize(self):
        if not self.token:
            self.last_error = "no token for the fan agent: set FAN_AGENT_TOKEN or put the token in fan_agent_token.txt next to config.yaml"
            raise FanError(self.last_error)
        self.status()                           # tells whether the agent answers

    def _set(self, on, reason):
        with self._lock:
            try:
                st = self._call("POST", "/fan", {"state": "on" if on else "off", "reason": reason[:200]})
            except FanError as exc:
                self.last_error = str(exc)
                self.requested = None           # unknown: the command may or may not have arrived
                raise
            if self.requested != on:
                log.info("fan %s via %s - %s", "ON" if on else "OFF", self.url, reason)
            self.requested, self.last_error, self.updated, self.last = on, None, _now(), st
            return self._from(st)

    def _from(self, st):
        return {"requested": st.get("requested", self.requested), "signal": st.get("signal"),
                "running": st.get("running"), "ok": self.last_error is None, "error": self.last_error,
                "updated": self.updated, "controller": f"Raspberry Pi agent {self.url}",
                "agent": {k: st.get(k) for k in ("pin", "active_high", "watchdog_seconds",
                                                 "on_comm_loss", "last_reason", "since")}}

    def status(self):
        """Also the heartbeat that keeps the agent's watchdog quiet."""
        with self._lock:
            try:
                st = self._call("GET", "/status")
                self.last_error, self.last = None, st
                if self.requested is None:
                    self.requested = st.get("requested")
            except FanError as exc:
                self.last_error = str(exc)
                return {"requested": self.requested, "signal": None, "running": None, "ok": False,
                        "error": self.last_error, "updated": self.updated,
                        "controller": f"Raspberry Pi agent {self.url}"}
            return self._from(st)


def make_controller(fan_cfg, token=None):
    kind = fan_cfg["controller"]
    if kind == "gpio":
        g = fan_cfg["gpio"]
        return GpioFanController(g["pin"], g["active_high"], False, g.get("feedback_pin"))
    if kind == "remote":
        r = fan_cfg["remote"]
        return RemoteFanController(r["url"], token, r["timeout_seconds"], r["retries"])
    return MockFanController()
