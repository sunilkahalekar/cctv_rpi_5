#!/usr/bin/env python3
# Copy of the Raspberry Pi 4 fan agent, used by the tests in --mock mode as a stand-in for the
# ESP32 agent (same network API). Not part of the Pi 5 system.
"""
Exhaust-fan agent for the Raspberry Pi 4: the only program that owns the relay pin.

The lab PC (camera app) sends "on" / "off" over the lab network; this agent sets GPIO 9
(BCM numbering, header pin 21) HIGH / LOW and reports back the pin level it reads.

    python3 pi_fan_agent.py                       # GPIO 9, active-HIGH relay, port 8095
    python3 pi_fan_agent.py --mock                # no GPIO: try it on any computer
    python3 pi_fan_agent.py --pin 9 --active-low  # relay module that switches on LOW

Security: every request needs the token (header "Authorization: Bearer <token>"). On the
first start a random token is written to fan_agent_token.txt (readable only by this
user) and printed once; put the same token on the lab PC in the environment variable
FAN_AGENT_TOKEN. --allow limits which computers may connect. The connection is plain HTTP:
keep it on the lab network.

Watchdog: the camera app asks for the status every few seconds. If nothing arrives for
--watchdog-seconds, --on-comm-loss is applied (off by default, the lab's choice; "on" or
"hold" are possible). At start the relay is OFF (--startup) until the app sends a command.

Endpoints: GET /status    POST /fan  {"state": "on"|"off", "reason": "..."}
Needs: Python 3.7+, RPi.GPIO (preinstalled on Raspberry Pi OS). Only the standard library
otherwise. fan_controller.py must be in the same folder.
"""
import argparse
import hmac
import json
import logging
import logging.handlers
import os
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from fan_controller import FanError, GpioFanController, MockFanController  # noqa: E402

log = logging.getLogger("iaqcam.fan")


def load_token(path):
    env = os.environ.get("FAN_AGENT_TOKEN")
    if env:
        return env.strip()
    if path.exists():
        return path.read_text().strip()
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    print(f"New token written to {path}:\n\n    {token}\n\nSet it on the lab PC:  set FAN_AGENT_TOKEN={token}\n")
    return token


class Agent:
    def __init__(self, controller, watchdog, on_comm_loss):
        self.c = controller
        self.watchdog = float(watchdog)
        self.on_comm_loss = on_comm_loss
        self.last_contact = time.time()
        self.lost = False
        self.last_reason = "agent started"
        self.since = time.strftime("%Y-%m-%d %H:%M:%S")
        self.lock = threading.Lock()

    def status(self):
        st = self.c.status()
        st.update(pin=getattr(self.c, "pin", None), active_high=getattr(self.c, "active_high", None),
                  watchdog_seconds=self.watchdog, on_comm_loss=self.on_comm_loss,
                  last_reason=self.last_reason, since=self.since, comm_lost=self.lost)
        return st

    def contact(self):
        with self.lock:
            self.last_contact = time.time()
            if self.lost:
                log.info("lab PC is back")
            self.lost = False

    def set(self, on, reason):
        with self.lock:
            st = self.c.fan_on(reason) if on else self.c.fan_off(reason)
            if st.get("requested") != on or self.last_reason != reason:
                self.since = time.strftime("%Y-%m-%d %H:%M:%S")
            self.last_reason = reason
            return st

    def watch(self):
        while True:
            time.sleep(2)
            if self.watchdog <= 0 or self.lost:
                continue
            if time.time() - self.last_contact > self.watchdog:
                self.lost = True
                msg = f"no contact from the lab PC for {self.watchdog:.0f} s"
                if self.on_comm_loss == "hold":
                    log.warning("%s: keeping the relay as it is", msg)
                else:
                    try:
                        self.set(self.on_comm_loss == "on", msg + f" (comm-loss action: {self.on_comm_loss})")
                    except FanError as exc:
                        log.error("comm-loss action failed: %s", exc)


def make_handler(agent, token, allow):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pi-fan-agent/1"

        def log_message(self, fmt, *args):          # quiet access log
            pass

        def _send(self, code, body):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self):
            if allow and self.client_address[0] not in allow:
                self._send(403, {"error": "this computer is not allowed"})
                return False
            got = self.headers.get("Authorization", "")
            if not hmac.compare_digest(got.encode(), ("Bearer " + token).encode()):
                time.sleep(0.5)
                self._send(401, {"error": "wrong or missing token"})
                return False
            agent.contact()
            return True

        def do_GET(self):
            if self.path != "/status":
                return self._send(404, {"error": "not found"})
            if self._authorised():
                self._send(200, agent.status())

        def do_POST(self):
            if self.path != "/fan":
                return self._send(404, {"error": "not found"})
            if not self._authorised():
                return
            try:
                n = min(int(self.headers.get("Content-Length", 0)), 4096)
                body = json.loads(self.rfile.read(n) or b"{}")
                state = body.get("state")
                if state not in ("on", "off"):
                    return self._send(400, {"error": "state must be 'on' or 'off'"})
                reason = str(body.get("reason") or "")[:200]
                agent.set(state == "on", f"{reason} [from {self.client_address[0]}]")
                self._send(200, agent.status())
            except FanError as exc:
                self._send(503, {"error": str(exc)})
            except ValueError:
                self._send(400, {"error": "body must be JSON"})

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8095)
    ap.add_argument("--pin", type=int, default=9, help="BCM GPIO number of the relay input (default 9)")
    ap.add_argument("--active-low", action="store_true", help="relay switches on with LOW")
    ap.add_argument("--feedback-pin", type=int, default=None, help="optional input that reads the fan's real state")
    ap.add_argument("--startup", choices=("off", "on"), default="off")
    ap.add_argument("--watchdog-seconds", type=float, default=120, help="0 = no watchdog")
    ap.add_argument("--on-comm-loss", choices=("off", "on", "hold"), default="off")
    ap.add_argument("--allow", nargs="*", default=[], help="IP addresses allowed to connect (default: all with the token)")
    ap.add_argument("--token-file", default=str(HERE / "fan_agent_token.txt"))
    ap.add_argument("--log", default=str(HERE / "fan_agent.log"))
    ap.add_argument("--mock", action="store_true", help="no GPIO (testing)")
    args = ap.parse_args()

    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for h in (logging.StreamHandler(), logging.handlers.RotatingFileHandler(args.log, maxBytes=2_000_000, backupCount=3)):
        h.setFormatter(fmt)
        log.addHandler(h)

    token = load_token(Path(args.token_file))
    controller = MockFanController() if args.mock else GpioFanController(
        args.pin, not args.active_low, args.startup == "on", args.feedback_pin)
    try:
        controller.initialize()
    except FanError as exc:
        sys.exit(f"Cannot start: {exc}\n(on a Raspberry Pi: is RPi.GPIO installed and is the user in the 'gpio' group?)")
    if args.mock and args.startup == "on":
        controller.fan_on("startup")
    agent = Agent(controller, args.watchdog_seconds, args.on_comm_loss)
    threading.Thread(target=agent.watch, daemon=True).start()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(agent, token, set(args.allow)))

    def stop(*_):
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    log.info("fan agent on port %d, %s, relay %s at start, watchdog %ss -> %s", args.port,
             "MOCK" if args.mock else f"GPIO {args.pin} active-{'LOW' if args.active_low else 'HIGH'}",
             args.startup.upper(), args.watchdog_seconds, args.on_comm_loss)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        controller.cleanup()                  # relay OFF, GPIO released
        log.info("fan agent stopped")


if __name__ == "__main__":
    main()
