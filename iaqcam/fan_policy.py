"""
AI mode of the exhaust fan: when to switch it on and off. Pure logic, no hardware and no
database, so every rule can be tested with made-up readings (tests/test_fan_policy.py).

Every check gets, per controlled pollutant, the newest measured value with its time and
the newest forecast for the forecast horizon with whether its model is trusted (it beat
"no change" on test minutes and is not paused).

Switch ON (fan off) when
  * a measured value has stayed >= its `on` threshold for `activation_seconds`
    (a single high reading does not switch the fan), or
  * forecast_trigger: a trusted forecast >= `on` in `forecast_confirmations` consecutive
    new forecasts, and the fan has been off for at least `min_off_seconds`.
    (Measured exceedances are not delayed by min_off_seconds.)
Switch OFF (fan on) only when
  * every controlled pollutant with a reading is <= its `off` threshold (below `on`:
    hysteresis) continuously for `recovery_seconds`, and
  * the fan has run for at least `min_on_seconds`, and
  * no forecast trigger is active.
  A falling forecast never switches the fan off: recovery must be measured.
Stale data: when the newest reading is older than `stale_seconds` (or there is none),
`stale_action` applies at once: off (this lab's choice), on, or hold. It overrides the
timers because it is a fault state.
"""
from datetime import datetime


class FanPolicy:
    def __init__(self, ai_cfg, labels=None):
        self.cfg = ai_cfg
        self.limits = {k: (float(v["start"]), float(v["stop"])) for k, v in (ai_cfg.get("pollutants") or {}).items()}
        self.labels = labels or {}
        self.above_since = {}          # pollutant -> first time of the current exceedance
        self.below_since = None        # time since when all readings are below 'off'
        self.fc_count = {}             # pollutant -> consecutive trusted forecasts above 'on'
        self.fc_seen = {}              # pollutant -> issue time of the last forecast counted

    def _name(self, key):
        return self.labels.get(key, key)

    def reset_timers(self):
        self.above_since, self.below_since, self.fc_count, self.fc_seen = {}, None, {}, {}

    def decide(self, now, readings, forecasts, fan_on, last_change):
        """
        now: datetime; readings: {pollutant: (value, datetime)}; forecasts: {pollutant:
        (value, issued_at, trusted)}; fan_on: current requested state (None = unknown, taken
        as off); last_change: datetime of the last switch or None.
        Returns {"on": True/False/None (None = keep), "reason": text, "details": dict}.
        """
        c = self.cfg
        fan_on = bool(fan_on)
        since_change = (now - last_change).total_seconds() if last_change else 1e9
        details = {"readings": {k: [v, t.strftime("%H:%M:%S")] for k, (v, t) in readings.items() if k in self.limits},
                   "forecasts": {k: [round(v, 2), str(t), trusted] for k, (v, t, trusted) in forecasts.items()
                                 if k in self.limits},
                   "limits": self.limits}

        usable = {k: v for k, v in readings.items() if k in self.limits and v[0] is not None}
        newest = max((t for _, t in usable.values()), default=None)
        if not self.limits:
            return {"on": None, "reason": "no pollutant thresholds configured (fan.ai.pollutants)", "details": details}
        age = (now - newest).total_seconds() if newest else None
        if age is None or age > c["stale_seconds"]:
            self.reset_timers()
            what = "no sensor reading" if age is None else f"sensor readings are {age / 60:.0f} min old"
            action = c["stale_action"]
            target = None if action == "hold" else (action == "on")
            return {"on": target, "stale": True, "details": details,
                    "reason": f"{what} (limit {c['stale_seconds'] / 60:.0f} min): fail-safe '{action}'"}

        # measured exceedances and recovery
        for k, (value, _) in usable.items():
            on_lim, _off = self.limits[k]
            if value >= on_lim:
                self.above_since.setdefault(k, now)
            else:
                self.above_since.pop(k, None)
        all_below = all(v <= self.limits[k][1] for k, (v, _) in usable.items())
        self.below_since = (self.below_since or now) if all_below else None

        # forecasts: count each new trusted forecast above 'on' once
        for k in self.limits:
            fc = forecasts.get(k)
            if fc is None or fc[0] is None:
                self.fc_count[k] = 0
                continue
            value, issued, trusted = fc
            if c.get("forecast_requires_model", True) and not trusted:
                self.fc_count[k] = 0
                continue
            if self.fc_seen.get(k) == issued:
                continue
            self.fc_seen[k] = issued
            self.fc_count[k] = self.fc_count.get(k, 0) + 1 if value >= self.limits[k][0] else 0

        measured = [k for k, t0 in self.above_since.items() if (now - t0).total_seconds() >= c["activation_seconds"]]
        pending = [k for k, t0 in self.above_since.items() if k not in measured]
        fc_hits = [k for k, n in self.fc_count.items() if c.get("forecast_trigger", True)
                   and n >= c["forecast_confirmations"]]

        def fmt(k):
            v = usable[k][0]
            return f"{self._name(k)} {v:g} >= {self.limits[k][0]:g}"

        if not fan_on:
            if measured:
                return {"on": True, "details": details, "reason": "measured: " + ", ".join(
                    f"{fmt(k)} for {(now - self.above_since[k]).total_seconds() / 60:.1f} min" for k in measured)}
            if fc_hits:
                if since_change < c["min_off_seconds"]:
                    return {"on": None, "details": details, "reason":
                            f"forecast above threshold, waiting for minimum off-time "
                            f"({since_change / 60:.1f} of {c['min_off_seconds'] / 60:.1f} min)"}
                return {"on": True, "details": details, "reason": "forecast: " + ", ".join(
                    f"{self._name(k)} {forecasts[k][0]:.1f} expected >= {self.limits[k][0]:g} "
                    f"({self.fc_count[k]} forecasts in a row)" for k in fc_hits)}
            if pending:
                return {"on": None, "details": details, "reason": "above threshold, confirming: " + ", ".join(
                    f"{fmt(k)} for {(now - self.above_since[k]).total_seconds():.0f} of "
                    f"{c['activation_seconds']:.0f} s" for k in pending)}
            return {"on": None, "details": details, "reason": "all pollutants below their ON thresholds"}

        # fan is on
        if not all_below:
            high = [k for k, (v, _) in usable.items() if v > self.limits[k][1]]
            return {"on": None, "details": details, "reason": "keeps running: " + ", ".join(
                f"{self._name(k)} {usable[k][0]:g} > recovery {self.limits[k][1]:g}" for k in high)}
        recovered = (now - self.below_since).total_seconds()
        if recovered < c["recovery_seconds"]:
            return {"on": None, "details": details, "reason":
                    f"recovering: all below recovery thresholds for {recovered / 60:.1f} of "
                    f"{c['recovery_seconds'] / 60:.1f} min"}
        if since_change < c["min_on_seconds"]:
            return {"on": None, "details": details, "reason":
                    f"recovered, minimum run time {since_change / 60:.1f} of {c['min_on_seconds'] / 60:.1f} min"}
        if fc_hits:
            return {"on": None, "details": details, "reason": "recovered, but the forecast is above threshold: "
                    + ", ".join(self._name(k) for k in fc_hits)}
        return {"on": False, "details": details, "reason":
                f"recovered: all pollutants below recovery thresholds for {recovered / 60:.1f} min"}


def parse_time(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
