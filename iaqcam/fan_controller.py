"""
Exhaust-fan controllers: one interface, three implementations.

    MockFanController    no hardware; for tests and for running without a Pi
    GpioFanController    drives the relay pin of the Raspberry Pi itself (RPi.GPIO, BCM)
    RemoteFanController  talks to pi_fan_agent.py on the Raspberry Pi (or the ESP32 sketch,
                         same API) over the LAN
    DualFanController    Raspberry Pi and ESP32 as alternatives: one active, the other on
                         standby (kept OFF); automatic or dashboard switch-over

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

    def __init__(self, url, token, timeout=3.0, retries=2, label=None):
        super().__init__()
        self.label = label or "Raspberry Pi agent"
        self.url = url.rstrip("/")
        self.token = token or ""
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.last = {}

    def _call(self, method, path, body=None, retries=None):
        data = json.dumps(body).encode() if body is not None else None
        error = None
        for attempt in range((self.retries if retries is None else retries) + 1):
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
                "updated": self.updated, "controller": f"{self.label} {self.url}",
                "agent": {k: st.get(k) for k in ("pin", "active_high", "watchdog_seconds",
                                                 "on_comm_loss", "last_reason", "since")}}

    def status(self):
        """Also the heartbeat that keeps the agent's watchdog quiet. One attempt only: an
        unreachable agent must not hold up the dashboard (the next check comes in seconds)."""
        with self._lock:
            try:
                st = self._call("GET", "/status", retries=0)
                self.last_error, self.last = None, st
                if self.requested is None:
                    self.requested = st.get("requested")
            except FanError as exc:
                self.last_error = str(exc)
                return {"requested": self.requested, "signal": None, "running": None, "ok": False,
                        "error": self.last_error, "updated": self.updated,
                        "controller": f"{self.label} {self.url}"}
            return self._from(st)

    def probe(self):
        """Quick status for a standby controller: one attempt, no retries."""
        with self._lock:
            try:
                st = self._call("GET", "/status", retries=0)
                self.last_error, self.last = None, st
                self.requested = st.get("requested")
            except FanError as exc:
                self.last_error = str(exc)
                return {"ok": False, "error": self.last_error, "requested": None, "signal": None}
            return self._from(st)


class DualFanController(FanController):
    """Two agents with the same API (Raspberry Pi and ESP32), both wired to the fan (two SSRs
    in parallel, or the fan moved by hand). Only the ACTIVE one is commanded; the standby
    one is kept OFF, so it can never hold the fan on. With failover "auto", a command or
    heartbeat that fails on the active agent moves control to the standby agent (if it
    answers) and repeats the last requested state there. Each agent switches its own relay
    OFF when the PC stops talking to it (watchdog), so an unreachable agent goes OFF too."""
    kind = "dual"

    def __init__(self, agents, active=None, failover="auto", on_switch=None):
        super().__init__()
        self.agents = dict(agents)                 # name -> RemoteFanController
        self.names = list(self.agents)
        self.active = active if active in self.agents else self.names[0]
        self.failover = failover
        self.on_switch = on_switch                 # callback(text) for the event log
        self.standby_status = {}

    @property
    def standby(self):
        return [n for n in self.names if n != self.active]

    def _note(self, text):
        log.warning(text)
        if self.on_switch:
            try:
                self.on_switch(text)
            except Exception:                      # the log must never break the control
                pass

    def initialize(self):
        errors = {}
        for name in [self.active] + self.standby:
            try:
                self.agents[name].initialize()
                if name != self.active:
                    if self.failover != "auto":
                        continue
                    old = self.active
                    self.active = name
                    self._note(f"fan control moved from {old} to {name}: {old} not available ({errors[old]})")
                self._keep_standby_off()
                return
            except FanError as exc:
                errors[name] = str(exc)
                if self.failover != "auto":
                    break
        self.last_error = "; ".join(f"{n}: {e}" for n, e in errors.items())
        raise FanError(self.last_error)

    def _keep_standby_off(self):
        for n in self.standby:
            st = self.agents[n].probe()
            if st.get("ok") and st.get("requested"):
                try:
                    self.agents[n].fan_off(f"standby: {self.active} controls the fan")
                    st = self.agents[n].probe()
                except FanError as exc:
                    st = {"ok": False, "error": str(exc)}
            self.standby_status[n] = st

    def _switch(self, to, reason, on=None):
        old = self.active
        self.active = to
        self._note(f"fan control moved from {old} to {to}: {reason}")
        try:                                       # best effort: the old one must not hold the fan on
            self.agents[old].fan_off(f"control moved to {to}")
        except FanError:
            pass                                   # unreachable: its own watchdog switches it OFF
        if on is not None:
            try:
                self.agents[to]._set(on, f"{reason} (repeated on {to})")
            except FanError:
                self.active = old
                raise

    def _set(self, on, reason):
        try:
            st = self.agents[self.active]._set(on, reason)
        except FanError as exc:
            if self.failover != "auto":
                self.last_error = str(exc)
                raise
            for n in self.standby:
                try:
                    self.agents[n].status()
                    if self.agents[n].last_error:
                        continue
                    self._switch(n, f"{self.active} failed ({exc})", on)
                    self.requested, self.last_error = on, None
                    return self.status()
                except FanError:
                    continue
            self.last_error = f"{self.active}: {exc}; no standby controller answers"
            raise FanError(self.last_error)
        self.requested, self.last_error, self.updated = on, None, _now()
        return self._decorate(st)

    def select(self, name, reason="selected on the dashboard"):
        """Move control to `name` and repeat the current requested state there."""
        if name not in self.agents:
            raise FanError(f"unknown controller {name!r}")
        if name == self.active:
            return self.status()
        new = self.agents[name]
        new.initialize()                           # raises when it does not answer
        want = self.requested
        self._switch(name, reason, want)
        return self.status()

    def _decorate(self, st):
        st = dict(st)
        st["active"] = self.active
        st["controllers"] = {}
        for n in self.names:
            a = self.agents[n]
            src = st if n == self.active else self.standby_status.get(n, {})
            st["controllers"][n] = {"url": a.url, "label": a.label, "active": n == self.active,
                                    "ok": bool(src.get("ok")), "error": src.get("error"),
                                    "requested": src.get("requested"), "signal": src.get("signal")}
        st["failover"] = self.failover
        return st

    def status(self):
        st = self.agents[self.active].status()     # heartbeat of the active agent
        if not st.get("ok") and self.failover == "auto":
            for n in self.standby:
                probe = self.agents[n].probe()
                if probe.get("ok"):
                    want = self.requested
                    try:
                        self._switch(n, f"{self.active} not reachable ({st.get('error')})", want)
                        st = self.agents[self.active].status()
                    except FanError as exc:
                        st = {"ok": False, "error": str(exc)}
                    break
        self._keep_standby_off()
        if st.get("ok") and self.requested is None:
            self.requested = st.get("requested")
        self.last_error = None if st.get("ok") else st.get("error")
        return self._decorate(st)

    def cleanup(self):
        for a in self.agents.values():
            a.cleanup()


class Esp32FanController(RemoteFanController):
    """The ESP32 fan agent (esp32_fan_agent.ino) as the ONLY fan controller (fan: controller:
    esp32), as on the Raspberry Pi 5 system: same network API as the Pi agent, so the remote
    logic is reused; the dashboard is told the ESP32 is the active board."""
    kind = "esp32"

    def __init__(self, url, token, timeout=3.0, retries=1):
        super().__init__(url, token, timeout, retries, label="ESP32 agent")

    def initialize(self):
        if not self.token:
            self.last_error = ("no token for the ESP32 agent: set ESP32_AGENT_TOKEN or put the token "
                               "(TOKEN in esp32_fan_agent/secrets.h) in esp32_agent_token.txt next to the config")
            raise FanError(self.last_error)
        self.status()

    def _from(self, st):
        out = super()._from(st)
        out["active"] = "esp32"
        return out

    def status(self):
        out = super().status()
        out["active"] = "esp32"
        return out

    def probe(self):
        out = super().probe()
        out["active"] = "esp32"
        return out


def make_controller(fan_cfg, token=None, tokens=None, active=None, on_switch=None):
    """tokens: {"pi": ..., "esp32": ...} for the dual controller."""
    kind = fan_cfg["controller"]
    if kind == "dual":
        r, e = fan_cfg["remote"], fan_cfg["esp32"]
        tokens = tokens or {}
        agents = {"pi": RemoteFanController(r["url"], tokens.get("pi", token), r["timeout_seconds"], r["retries"],
                                            label="Raspberry Pi agent"),
                  "esp32": RemoteFanController(e["url"], tokens.get("esp32"), e["timeout_seconds"], e["retries"],
                                               label="ESP32 agent")}
        return DualFanController(agents, active or fan_cfg.get("preferred", "pi"), fan_cfg.get("failover", "auto"),
                                 on_switch)
    if kind == "esp32":
        e = fan_cfg["esp32"]
        return Esp32FanController(e["url"], (tokens or {}).get("esp32") or token, e["timeout_seconds"],
                                  e["retries"])
    if kind == "gpio":
        g = fan_cfg["gpio"]
        return GpioFanController(g["pin"], g["active_high"], False, g.get("feedback_pin"))
    if kind == "remote":
        r = fan_cfg["remote"]
        return RemoteFanController(r["url"], token, r["timeout_seconds"], r["retries"])
    return MockFanController()
