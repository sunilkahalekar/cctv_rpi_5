"""Poll a ThingSpeak channel and store its entries (read-only use of the channel)."""
import threading
from datetime import datetime, timezone

import requests

from .db import FIELDS


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_feed(payload):
    """ThingSpeak feeds.json -> (channel name, {field: name}, [row dicts])."""
    channel = payload.get("channel") or {}
    names = {f: str(channel[f]).strip() for f in FIELDS if channel.get(f)}
    rows = []
    for entry in payload.get("feeds") or []:
        if entry.get("entry_id") is None or not entry.get("created_at"):
            continue
        utc = datetime.fromisoformat(str(entry["created_at"]).replace("Z", "+00:00"))
        if utc.tzinfo is None:
            utc = utc.replace(tzinfo=timezone.utc)
        local = utc.astimezone().replace(tzinfo=None)
        row = {
            "entry_id": int(entry["entry_id"]),
            "created_at_utc": utc.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "ts_local": local.strftime("%Y-%m-%d %H:%M:%S"),
            "window_start": local.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for f in FIELDS:
            row[f] = _to_float(entry.get(f))
        rows.append(row)
    return channel.get("name"), names, rows


class ThingSpeakPoller(threading.Thread):
    def __init__(self, cfg, db, state):
        super().__init__(daemon=True, name="thingspeak")
        ts = cfg["thingspeak"]
        self.url = f"{str(ts['base_url']).rstrip('/')}/channels/{ts['channel_id']}/feeds.json"
        self.api_key = ts.get("read_api_key") or ""
        self.poll_seconds = max(5, int(ts["poll_seconds"]))
        self.backfill = min(8000, max(1, int(ts["backfill_results"])))
        self.name_overrides = {k: v for k, v in (ts.get("field_names") or {}).items() if v}
        self.db = db
        self.state = state
        self._stop_event = threading.Event()
        self.failing = False

    def stop(self):
        self._stop_event.set()

    def poll_once(self, results):
        params = {"results": results}
        if self.api_key:
            params["api_key"] = self.api_key
        response = requests.get(self.url, params=params, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or "feeds" not in payload:
            raise ValueError(f"unexpected reply from ThingSpeak: {str(payload)[:120]}")
        channel_name, names, rows = parse_feed(payload)
        names.update(self.name_overrides)
        if names:
            self.db.write_sensor_fields(names)
        new = self.db.write_sensor_entries(rows)
        self.state.update_sensors(last_ok=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                  last_error=None, channel_name=channel_name)
        return new

    def run(self):
        catch_up = True     # after start-up or an outage, fetch history (duplicates are ignored)
        while not self._stop_event.is_set():
            try:
                new = self.poll_once(self.backfill if catch_up else 100)
                if catch_up:
                    print(f"[thingspeak] connected, {new} new entries stored")
                    self.db.write_event("sensor", f"ThingSpeak channel read again, {new} new entries stored"
                                        if self.failing else f"ThingSpeak connected, {new} new entries stored")
                catch_up, self.failing = False, False
            except Exception as exc:                      # keep polling whatever happens
                catch_up = True
                self.state.update_sensors(last_error=f"{type(exc).__name__}: {exc}"[:200])
                print(f"[thingspeak] read failed: {type(exc).__name__}: {str(exc)[:160]}")
                if not self.failing:
                    self.db.write_event("sensor", f"ThingSpeak read failed: {type(exc).__name__}: {str(exc)[:200]}",
                                        level="warning")
                self.failing = True
            self._stop_event.wait(self.poll_seconds)
