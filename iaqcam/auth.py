"""Sign-in for the dashboard: user file with hashed passwords, session key, attempt limit."""
import json
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,40}$")
MIN_PASSWORD = 8
# checked when the user name does not exist, so both cases take the same time
_DUMMY = generate_password_hash(secrets.token_hex(8))


class UserStore:
    """users.json: {"name": {"hash": "<salted hash>"}}. Passwords are never stored."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _read(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def names(self):
        return sorted(self._read())

    def add(self, name, password):
        if not NAME_RE.match(name or ""):
            raise ValueError("user name: 1-40 letters, digits or . _ - @")
        if len(password or "") < MIN_PASSWORD:
            raise ValueError(f"password: at least {MIN_PASSWORD} characters")
        with self._lock:
            users = self._read()
            users[name] = {"hash": generate_password_hash(password)}
            self.path.write_text(json.dumps(users, indent=2), encoding="utf-8")

    def remove(self, name):
        with self._lock:
            users = self._read()
            if name not in users:
                return False
            del users[name]
            self.path.write_text(json.dumps(users, indent=2), encoding="utf-8")
            return True

    def verify(self, name, password):
        user = self._read().get(name or "")
        ok = check_password_hash(user["hash"] if user else _DUMMY, password or "")
        return bool(user) and ok


def secret_key(path):
    """Key that signs the session cookie; made once and kept so sign-ins survive a restart."""
    path = Path(path)
    try:
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    path.write_text(key, encoding="utf-8")
    return key


class Throttle:
    """At most `limit` failed sign-ins per address in `window` seconds."""

    def __init__(self, limit=5, window=300):
        self.limit, self.window = limit, window
        self._fails = defaultdict(deque)
        self._lock = threading.Lock()

    def blocked(self, who):
        with self._lock:
            fails = self._fails[who]
            while fails and time.time() - fails[0] > self.window:
                fails.popleft()
            return len(fails) >= self.limit

    def failed(self, who):
        with self._lock:
            self._fails[who].append(time.time())

    def clear(self, who):
        with self._lock:
            self._fails.pop(who, None)
