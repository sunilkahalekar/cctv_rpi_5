"""
Owner of the exhaust fan: the only component that sends commands to the fan controller.

Modes (mutually exclusive, saved in the database so they survive a restart):
  manual  only the dashboard's ON / OFF buttons switch the fan; no AI decisions are made.
  ai      FanPolicy decides every `check_seconds`; the buttons are refused.

Mode changes
  manual -> ai   the policy starts from the current fan state, the current readings and
                 forecasts, and decides at once (minimum run / off times still count from
                 the last real switch). Confirmation timers start from zero.
  ai -> manual   the AI stops issuing commands immediately; the fan keeps its state until
                 somebody presses ON or OFF.
Restart
  manual         the last manual command is sent again.
  ai             the policy starts from the state the controller reports.
Failures
  A failed command is shown on the dashboard and logged; the requested state becomes
  "unknown" and is sent again at the next check. If the app stops talking to the
  Raspberry Pi, the agent's own watchdog applies its comm-loss action (pi_fan_agent.py).

All commands pass one lock, so a manual command and an AI decision can never interleave;
a second button press while a command is still running is refused, not queued.

Independent of the mode, every check also logs pollutant threshold crossings, sensor
data going stale / fresh again and controller connection changes to the event log.
"""
import logging
import os
import re
import threading
from datetime import datetime

from .db import FIELDS, read_connection
from .fan_controller import FanError, MockFanController, make_controller
from .fan_policy import FanPolicy, parse_time
from .sensor_meta import canonical, describe

log = logging.getLogger("iaqcam.fan")
FMT = "%Y-%m-%d %H:%M:%S"
KIND = {"mode": "fan_mode", "command": "fan_command", "decision": "fan_decision",
        "error": "fan_error", "startup": "system", "controller": "controller"}


def fan_token(remote):
    """Token of the Pi fan agent: the environment variable (token_env), else the first line of
    token_file (default fan_agent_token.txt next to config.yaml; never uploaded, see .gitignore)."""
    def clean(t):      # tolerate "Bearer x", "FAN_AGENT_TOKEN=x", "set FAN_AGENT_TOKEN=x" and quotes
        t = t.strip().strip('"').strip()
        if t.lower().startswith("bearer "):
            t = t[7:]
        if "=" in t:                     # tokens never contain "=" (URL-safe base64 without padding)
            t = t.rsplit("=", 1)[1]
        return t.strip().strip('"').strip()
    token = clean(os.environ.get(remote.get("token_env") or "FAN_AGENT_TOKEN", ""))
    if token:
        return token
    path = remote.get("token_file") or "fan_agent_token.txt"
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return clean(fh.readline())
    except OSError:
        return ""


class FanService(threading.Thread):
    def __init__(self, cfg, db, controller=None, clock=None):
        super().__init__(daemon=True, name="fan")
        self.cfg = cfg["fan"]
        self.db = db
        self.db_path = cfg["database"]["path"]
        self.clock = clock or datetime.now
        self.dry_run = bool(self.cfg.get("dry_run"))
        token = fan_token(self.cfg["remote"])
        if controller is None:
            if self.dry_run:
                controller = MockFanController()
            else:
                saved_active = db.get_state("fan_controller_active")
                controller = make_controller(self.cfg, token,
                                             tokens={"pi": token, "esp32": fan_token(self.cfg["esp32"])},
                                             active=saved_active, on_switch=self._switched)
        self.controller = controller
        self.policy = FanPolicy(self.cfg["ai"])
        self._lock = threading.RLock()
        self._snap_lock = threading.Lock()
        self._stop_event = threading.Event()
        self.initialized = False
        self.controller_ok = None
        self.stale = None
        self.above = {}                     # pollutant -> True while above its start threshold
        self.last_command = None

        saved = db.get_state("fan_mode")
        start = self.cfg["startup_mode"]
        self.mode = (saved if saved in ("manual", "ai") else self.cfg["default_mode"]) if start == "last" else start
        self.manual_state = db.get_state("fan_manual") == "on"
        lc = db.get_state("fan_last_change")
        self.last_change = parse_time(lc) if lc else None
        self.last_decision = {"reason": "starting", "at": None}
        self.snapshot = {}
        self._event("startup", None, True, f"fan control started in {self.mode} mode ("
                    f"{'DRY RUN, no relay' if self.dry_run else self.controller.kind + ' controller'})")

    # ------------------------------------------------------------ helpers
    def _switched(self, text):
        """Called by the dual controller when control moves between Raspberry Pi and ESP32."""
        try:
            self.db.set_state("fan_controller_active", self.controller.active)
        except Exception:
            pass
        self._event("controller", None, True, text, level="warning")

    def _event(self, kind, requested, ok, reason, user=None, details=None, level=None, pollutant=None):
        state = None if requested is None else ("on" if requested else "off")
        level = level or ("warning" if ok is False else "info")
        self.db.write_event(KIND.get(kind, kind), reason, level=level, pollutant=pollutant, fan_state=state,
                            mode=self.mode, ok=ok, user=user, details=details, ts=self.clock().strftime(FMT))
        (log.warning if level != "info" else log.info)("[%s] %s %s %s%s", self.mode, kind, state or "", reason,
                                                       f" (by {user})" if user else "")

    def _ensure_init(self):
        if self.initialized:
            return True
        try:
            self.controller.initialize()
            self.initialized = True
            self._controller_state(True, "controller ready")
            return True
        except FanError as exc:
            self._controller_state(False, str(exc))
            self._update_snapshot(error=str(exc))
            return False

    def _controller_state(self, ok, text):
        if ok != self.controller_ok:
            self._event("controller", None, ok, ("controller connected: " if ok else "controller not reachable: ") + text,
                        level="info" if ok else "critical")
            self.controller_ok = ok

    def _command(self, on, reason, kind, user=None, details=None):
        """Send one command. Caller holds self._lock. Returns (ok, message)."""
        if not self._ensure_init():
            self._event("error", on, False, f"command not sent, controller not available: {self.controller.last_error}", user)
            return False, f"Fan controller not available: {self.controller.last_error}"
        before = self.controller.get_requested_state()
        self.last_command = {"at": self.clock().strftime(FMT), "state": "on" if on else "off", "ok": None,
                             "reason": reason, "user": user}
        try:
            (self.controller.fan_on if on else self.controller.fan_off)(reason)
        except FanError as exc:
            self.last_command.update(ok=False, error=str(exc))
            self._controller_state(False, str(exc))
            self._event("error", on, False, f"command {'ON' if on else 'OFF'} failed: {exc}", user, details)
            self._update_snapshot()
            return False, f"Command failed: {exc}"
        self.last_command["ok"] = True
        self._controller_state(True, "command accepted")
        if before != on:
            self.last_change = self.clock()
            self.db.set_state("fan_last_change", self.last_change.strftime(FMT))
        self._event(kind, on, True, reason, user, details)
        self._update_snapshot()
        return True, "Fan " + ("ON" if on else "OFF") + " command accepted by the controller"

    def _update_snapshot(self, error=None):
        try:
            st = self.controller.status() if self.initialized else {
                "requested": None, "signal": None, "running": None, "ok": False,
                "error": error or self.controller.last_error, "controller": self.controller.kind}
        except Exception as exc:
            st = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if self.dry_run:
            st["controller"] = "DRY RUN: commands are simulated, no relay is switched"
        if self.cfg["controller"] == "esp32":             # the ESP32 is the only fan board
            st.setdefault("active", "esp32")
            gpio = {"pin": self.cfg["esp32"].get("pin", 26), "active_high": self.cfg["esp32"].get("active_high", True),
                    "source": "config"}
        else:
            gpio = {"pin": self.cfg["gpio"]["pin"], "active_high": self.cfg["gpio"]["active_high"], "source": "config.yaml"}
        agent = st.get("agent") or {}
        if agent.get("pin") is not None:
            gpio = {"pin": agent["pin"], "active_high": agent.get("active_high"),
                    "source": "ESP32 agent" if st.get("active") == "esp32" else "Raspberry Pi agent"}
        with self._snap_lock:
            self.snapshot = {**st, "mode": self.mode, "manual_state": self.manual_state, "dry_run": self.dry_run,
                             "last_change": self.last_change.strftime(FMT) if self.last_change else None,
                             "last_command": dict(self.last_command) if self.last_command else None,
                             "decision": dict(self.last_decision), "checked": self.clock().strftime(FMT),
                             "stale": self.stale, "gpio": gpio}

    def status(self):
        with self._snap_lock:
            return dict(self.snapshot)

    # ------------------------------------------------------- data for AI mode
    def _inputs(self):
        con = read_connection(self.db_path)
        try:
            names = dict(con.execute("SELECT field, name FROM sensor_fields").fetchall())
            models = {r["field"]: r["used"] for r in con.execute("SELECT field, used FROM forecast_models")}
            readings, forecasts, labels = {}, {}, {}
            now_text = self.clock().strftime(FMT)
            for f in FIELDS:
                key = canonical(names.get(f, ""))
                if not key or key not in self.policy.limits:
                    continue
                labels[key] = describe(names[f])["label"]
                r = con.execute(f"SELECT ts_local, {f} AS v FROM sensor_readings WHERE {f} IS NOT NULL "
                                f"ORDER BY entry_id DESC LIMIT 1").fetchone()
                if r:
                    readings[key] = (float(r["v"]), parse_time(r["ts_local"]))
                fc = con.execute("SELECT value, predicted_at FROM forecasts WHERE field = ? AND "
                                 "predicted_for > ? ORDER BY predicted_at DESC LIMIT 1", (f, now_text)).fetchone()
                if fc and fc["value"] is not None:
                    forecasts[key] = (float(fc["value"]), fc["predicted_at"], models.get(f) in ("full", "sensors", "physics", "greybox", "physreg"))
        finally:
            con.close()
        self.policy.labels = labels
        return readings, forecasts

    def _monitor(self, readings, now):
        """Threshold crossings and stale data, logged in every mode."""
        newest = max((t for _, t in readings.values()), default=None)
        stale = newest is None or (now - newest).total_seconds() > self.cfg["ai"]["stale_seconds"]
        if stale != self.stale:
            if stale:
                self._event("sensor", None, None, "sensor data stale: " + ("no reading" if newest is None else
                            f"last reading {newest.strftime(FMT)}"), level="warning")
            elif self.stale is not None:
                self._event("sensor", None, None, "sensor data fresh again", level="info")
            self.stale = stale
        for key, (value, _) in readings.items():
            start, stop = self.policy.limits[key]
            name = self.policy.labels.get(key, key)
            was = self.above.get(key)
            if value >= start and not was:
                self.above[key] = True
                self._event("threshold", None, None, f"{name} {value:g} reached its start threshold {start:g}",
                            level="warning", pollutant=key, details={"value": value, "start": start, "stop": stop})
            elif was and value <= stop:
                self.above[key] = False
                self._event("threshold", None, None, f"{name} {value:g} back at/below its stop threshold {stop:g}",
                            pollutant=key, details={"value": value, "start": start, "stop": stop})

    # ------------------------------------------------------------- the loop
    def step(self):
        with self._lock:
            now = self.clock()
            try:
                readings, forecasts = self._inputs()
            except Exception as exc:
                readings, forecasts = {}, {}
                log.warning("fan inputs not readable: %s", exc)
            self._monitor(readings, now)
            if not self._ensure_init():
                return
            st = self.controller.status()               # also the agent's heartbeat
            self._controller_state(bool(st.get("ok")), st.get("error") or "status ok")
            if self.mode == "manual":
                # re-send the manual state if it is unknown or differs (restart, lost command)
                if st.get("ok") and (st.get("requested") is None or st.get("requested") != self.manual_state):
                    self._command(self.manual_state, "manual state restored", "command")
                self.last_decision = {"reason": "Manual mode: the ON / OFF buttons decide; the AI is not issuing commands.",
                                      "at": now.strftime(FMT)}
            else:
                current = st.get("requested")
                d = self.policy.decide(now, readings, forecasts, current, self.last_change)
                # log a decision when its reason changes, not when only a number or timer in it changes
                gist = lambda text: re.sub(r"[0-9.]+", "#", text or "")
                changed_reason = gist(d["reason"]) != gist(self.last_decision.get("reason"))
                self.last_decision = {"reason": d["reason"], "at": now.strftime(FMT), "target": d["on"],
                                      "stale": bool(d.get("stale")),
                                      "readings": d["details"].get("readings"),
                                      "forecasts": d["details"].get("forecasts")}
                if d["on"] is not None and (current is None or bool(current) != d["on"]):
                    self._command(d["on"], ("AI: " if not d.get("stale") else "AI fail-safe: ") + d["reason"],
                                  "decision", details=d["details"])
                elif changed_reason:
                    self._event("decision", None, None, "AI: " + d["reason"], details=d["details"])
            self._update_snapshot()

    def _log_context(self):
        """Fan state of this minute for the physics of the room air (context_minute)."""
        st = self.status()
        req, sig = st.get("requested"), st.get("signal")
        minute = self.clock().strftime("%Y-%m-%d %H:%M:00")
        self.db.write_context_minute(minute, None if req is None else int(bool(req)),
                                     None if sig is None else int(bool(sig)), int(self.dry_run), self.mode)

    def run(self):
        while not self._stop_event.is_set():
            try:
                self.step()
                self._log_context()
            except Exception as exc:                  # never take the app down
                log.exception("fan step failed: %s", exc)
                self.db.write_event("error", f"fan control step failed: {type(exc).__name__}: {exc}", level="critical")
                self._update_snapshot(error=f"{type(exc).__name__}: {exc}")
            self._stop_event.wait(float(self.cfg["check_seconds"]))

    def stop(self):
        self._stop_event.set()

    def shutdown(self):
        """Controlled stop of the app. GPIO on this computer is released (relay OFF); a remote
        Pi keeps its state until its watchdog acts."""
        with self._lock:
            self._event("startup", None, True, "fan control stopped (app shutdown)")
            self.controller.cleanup()

    # ---------------------------------------------------------- dashboard API
    def set_mode(self, mode, user=None):
        if mode not in ("manual", "ai"):
            return False, "mode must be manual or ai"
        if not self._lock.acquire(timeout=15):
            return False, "the fan controller is busy, try again"
        try:
            if mode == self.mode:
                return True, f"already in {mode} mode"
            st = self.controller.status() if self.initialized else {}
            self.mode = mode
            self.db.set_state("fan_mode", mode)
            if mode == "manual":
                # the fan keeps its current state; the buttons take over from it
                self.manual_state = bool(st.get("requested"))
                self.db.set_state("fan_manual", "on" if self.manual_state else "off")
            else:
                self.policy.reset_timers()
            self._event("mode", None, True, f"mode changed to {'AI' if mode == 'ai' else 'Manual'}", user)
            self.step()                                # AI: evaluate at once from the current data
        finally:
            self._lock.release()
        return True, f"{'AI' if mode == 'ai' else 'Manual'} mode"

    def select_controller(self, name, user=None):
        """Dashboard: move fan control to the Raspberry Pi or the ESP32 (dual controller)."""
        if getattr(self.controller, "kind", "") != "dual":
            return False, "only one fan controller is configured (fan: controller: dual for Pi + ESP32)"
        if not self._lock.acquire(timeout=15):
            return False, "the fan controller is busy, try again"
        try:
            try:
                self.controller.select(name, f"selected on the dashboard by {user or 'operator'}")
            except FanError as exc:
                self._event("error", None, False, f"cannot move fan control to {name}: {exc}", user)
                return False, f"{name} not available: {exc}"
            self.db.set_state("fan_controller_active", self.controller.active)
            self.initialized = True
            self._update_snapshot()
            return True, f"fan control now via {name}"
        finally:
            self._lock.release()

    def manual_command(self, on, user=None):
        # wait for a running status check (a slow or unreachable controller) instead of refusing
        if not self._lock.acquire(timeout=20):
            return False, "Another fan command is still being carried out. Wait for its result."
        try:
            if self.mode != "manual":
                return False, "The fan is in AI mode: switch to Manual mode first."
            ok, msg = self._command(on, f"manual {'ON' if on else 'OFF'} button", "command", user)
            if ok:
                self.manual_state = on
                self.db.set_state("fan_manual", "on" if on else "off")
            return ok, msg
        finally:
            self._lock.release()

    def describe_policy(self):
        ai = self.cfg["ai"]
        return {"pollutants": {k: {"start": v["start"], "stop": v["stop"]} for k, v in (ai["pollutants"] or {}).items()},
                **{k: ai[k] for k in ("activation_seconds", "recovery_seconds", "min_on_seconds",
                                      "min_off_seconds", "stale_seconds", "stale_action", "forecast_trigger",
                                      "forecast_confirmations", "forecast_requires_model")},
                "check_seconds": self.cfg["check_seconds"]}
