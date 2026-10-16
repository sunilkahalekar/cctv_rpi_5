"""
Events from the physics of the room air and the grey-box forecast (event log kinds
"physics" and "model"). Written by the forecaster once a minute per pollutant, only when
something changes, so the log stays readable. The last state per pollutant is kept in
app_state, so a restart does not log everything again.

kind "model"   (which forecast is trusted)
  - the variant in use changes (physics / grey-box / physics-informed / data / no change)
  - the forecast is paused because its recent forecasts were worse than "no change", and
    when it resumes

kind "physics" (what the mass balance says about the room)
  - removal rate measured for the first time / changed by more than 20 % (fan off, fan on)
  - the exhaust fan's effect: removal with the fan on vs off (warning when < 1.3 x)
  - live air change much lower than usual while people are present (room sealed?)
  - at the current activity the level heads above the fan start threshold
    (steady level C_b + S/r), before the reading gets there
  - the pollutant rises while nobody is in the detection areas (machine, door, outside air)
  - CO2 per person from the camera count outside 0.15-0.6 L/min (count or volume wrong)
"""
import json

NAME = {"physics": "physics (mass balance)", "greybox": "grey-box (physics + learned rest)",
        "physreg": "physics-informed regression", "transit": "pollution in transit (camera zones + travel times)", "full": "data (readings, persons, motion)",
        "sensors": "data (sensor's own readings)", "no change": "no change (no model beats it yet)",
        "paused": "paused"}
HOLD_MINUTES = 3            # a condition must hold this many minutes before it is logged
SEALED_MINUTES = 10


class PhysicsEvents:
    def __init__(self, db, limits=None):
        self.db = db
        self.limits = limits or {}          # canonical kind -> {"start", "stop"}

    # ---------------------------------------------------------------- state
    def _load(self, kind):
        try:
            return json.loads(self.db.get_state(f"phys_ev_{kind}", "{}") or "{}")
        except (TypeError, ValueError):
            return {}

    def _save(self, kind, st):
        self.db.set_state(f"phys_ev_{kind}", json.dumps(st))

    def _log(self, kind, message, level="info", pol=None, details=None, fan_state=None, ts=None):
        self.db.write_event(kind, message, level=level, pollutant=pol, details=details, fan_state=fan_state, ts=ts)

    @staticmethod
    def _held(st, key, cond):
        """Consecutive minutes a condition has held (0 when it is false)."""
        st[key] = (st.get(key, 0) + 1) if cond else 0
        return st[key]

    # ---------------------------------------------------------------- rules
    def observe(self, kind, label, unit, info, ts=None):
        """info: used, paused, skill (dict), rates (dict, per minute), background, value,
        forecast, lambda_online (per minute or None), source, source_ref, persons, fan_on,
        co2_gen_lpm. Returns the number of events written."""
        st = self._load(kind)
        n = 0
        u = unit or ""

        # --- model: which forecast is in use -------------------------------
        used = "paused" if info.get("paused") else info.get("used")
        prev = st.get("used")
        if used and used != prev:
            skill = (info.get("skill") or {}).get(used)
            sk = f" (skill {skill:.2f} vs no change)" if skill is not None else ""
            if used == "paused":
                msg, lvl = (f"{label} forecast paused: its recent forecasts were worse than \"no change\"; "
                            f"the dashboard shows the current reading until it recovers"), "warning"
            elif prev == "paused":
                msg, lvl = f"{label} forecast resumed with {NAME.get(used, used)}{sk}", "info"
            elif prev is None:
                msg, lvl = f"{label} forecast uses {NAME.get(used, used)}{sk}", "info"
            else:
                msg, lvl = f"{label} forecast switched from {NAME.get(prev, prev)} to {NAME.get(used, used)}{sk}", "info"
            self._log("model", msg, lvl, kind, {"used": used, "previous": prev, "skill": info.get("skill")}, ts=ts)
            st["used"] = used
            n += 1

        # --- removal rates (decays) ---------------------------------------
        rates = info.get("rates") or {}
        measured = str(rates.get("source") or "").startswith("decay")
        what = "air change" if kind == "co2" else "removal (air change + settling)"
        if measured and rates.get("n_off") and rates.get("off"):
            off_h = rates["off"] * 60
            last = st.get("off_h")
            if last is None or abs(off_h - last) > 0.2 * last:
                change = "" if last is None else f" (was {last:.2f}/h)"
                self._log("physics", f"{label} {what} measured: {off_h:.2f} per hour with the fan off, "
                                     f"from {rates['n_off']} empty-room decays{change}", "info", kind,
                          {"removal_per_hour": round(off_h, 4), "decays": rates["n_off"]}, fan_state="off", ts=ts)
                st["off_h"] = off_h
                n += 1
        if rates.get("on") and rates.get("n_on"):
            on_h = rates["on"] * 60
            last = st.get("on_h")
            if last is None or abs(on_h - last) > 0.2 * last:
                # compared only with a MEASURED fan-off value (not the assumed default)
                off_h = st.get("off_h") or (rates["off"] * 60 if rates.get("off") and rates.get("n_off") else None)
                ratio = on_h / off_h if off_h else None
                weak = ratio is not None and ratio < 1.3
                msg = (f"{label} {what} with the exhaust fan on: {on_h:.2f} per hour"
                       + (f", {ratio:.1f} x the fan-off value" if ratio else ", fan-off value not measured yet")
                       + (": the fan barely helps (check the duct, filter or that the relay really switches)" if weak else ""))
                self._log("physics", msg, "warning" if weak else "info", kind,
                          {"removal_fan_per_hour": round(on_h, 4), "ratio": ratio}, fan_state="on", ts=ts)
                st["on_h"] = on_h
                n += 1

        # --- live air change much lower than usual (CO2, occupied) --------
        lam, base = info.get("lambda_online"), rates.get("off")
        occupied = (info.get("persons") or 0) > 0
        if kind == "co2" and measured and base:
            low = occupied and lam is not None and lam < 0.5 * base
            held = self._held(st, "low_n", low)
            if held >= SEALED_MINUTES and not st.get("sealed"):
                self._log("physics", f"Air change low: {lam * 60:.2f} per hour while occupied, usual "
                                     f"{base * 60:.2f} with the fan off. Doors, windows or AC closed? "
                                     f"Open the door or switch the fan on", "warning", kind,
                          {"lambda_online_per_hour": round(lam * 60, 4), "usual_per_hour": round(base * 60, 4)}, ts=ts)
                st["sealed"] = True
                n += 1
            elif st.get("sealed") and lam is not None and lam > 0.7 * base:
                self._log("physics", f"Air change back to normal: {lam * 60:.2f} per hour", "info", kind, ts=ts)
                st["sealed"] = False
                n += 1

        # --- steady level above the fan start threshold --------------------
        lim = self.limits.get(kind)
        r, s, cb, value = (rates.get("now"), info.get("source"), info.get("background"), info.get("value"))
        if lim and r and s is not None and cb is not None and value is not None:
            steady = cb + s / r
            ahead = steady >= lim["start"] and value < lim["start"]
            held = self._held(st, "steady_n", ahead)
            if held >= HOLD_MINUTES and not st.get("steady_alert"):
                self._log("physics", f"{label} heads to about {steady:.0f} {u} at the current activity "
                                     f"(fan start {lim['start']:g}); now {value:.0f}. The room needs more air "
                                     f"or less emitting work", "warning", kind,
                          {"steady": round(steady, 3), "value": value, "start": lim["start"],
                           "source_per_min": round(s, 4), "removal_per_hour": round(r * 60, 4)}, ts=ts)
                st["steady_alert"] = True
                n += 1
            elif st.get("steady_alert") and steady < lim["stop"]:
                self._log("physics", f"{label} steady level back below the fan stop threshold "
                                     f"({steady:.0f} {u} < {lim['stop']:g})", "info", kind, ts=ts)
                st["steady_alert"] = False
                n += 1

        # --- rising with nobody in the areas ---------------------------------
        ref = info.get("source_ref")
        if s is not None and ref is not None and info.get("persons") is not None:
            nobody = info["persons"] == 0 and s > ref
            held = self._held(st, "nobody_n", nobody)
            if held >= HOLD_MINUTES and not st.get("nobody_alert"):
                self._log("physics", f"{label} rising with nobody in the detection areas (source {s:.2f} {u}/min): "
                                     f"a machine running, a door to a dusty area or outside air?", "warning", kind,
                          {"source_per_min": round(s, 4), "usual_max": round(ref, 4)}, ts=ts)
                st["nobody_alert"] = True
                n += 1
            elif st.get("nobody_alert") and not nobody and held == 0 and s <= 0.5 * ref:
                st["nobody_alert"] = False

        # --- CO2 per person: a check of the camera count ---------------------
        gen = info.get("co2_gen_lpm")
        if kind == "co2" and gen is not None:
            odd = not 0.15 <= gen <= 0.6
            if odd and not st.get("gen_odd"):
                self._log("physics", f"CO2 per person from the camera count is {gen:.2f} L/min (about 0.3 expected): "
                                     f"check the detection areas and physics: volume_m3", "warning", kind,
                          {"co2_gen_lpm": gen}, ts=ts)
                st["gen_odd"] = True
                n += 1
            elif not odd and st.get("gen_odd"):
                self._log("physics", f"CO2 per person back in the expected range ({gen:.2f} L/min)", "info", kind, ts=ts)
                st["gen_odd"] = False
                n += 1

        self._save(kind, st)
        return n


class EarlyWarnings:
    """Early warnings from the pollution-in-transit paths, and their outcome when the sensor
    later crosses the threshold (or does not). Kept in early_warnings + event log kind
    "warning" so the lead time over the sensor can be measured."""

    CONFIRM_MINUTES = 2        # the 'continue' path must reach the threshold this many minutes in a row
    EXPIRE_MINUTES = 40        # a warning without a crossing after this long is a false alarm

    def __init__(self, db):
        self.db = db

    def _load(self, kind):
        try:
            return json.loads(self.db.get_state(f"ew_{kind}", "{}") or "{}")
        except (TypeError, ValueError):
            return {}

    def observe(self, field, kind, label, unit, now, value, start, stop, ttt_continue, peak_continue,
                peak_committed, ttt_fan=None, peak_fan=None, ttt_committed=None):
        """now: datetime of the newest sensor minute. ttt_continue / peak_continue: the
        'expected' path (activity continues as usual); ttt_committed: the path of what is
        already in the air (certain). Returns what happened (or None)."""
        if ttt_committed is not None and (ttt_continue is None or ttt_committed < ttt_continue):
            ttt_continue = ttt_committed
        from datetime import datetime
        st = self._load(kind)
        fmt = "%Y-%m-%d %H:%M:%S"
        now_s = now.strftime(fmt)
        happened = None
        if value >= start:
            if not st.get("crossed"):
                act = st.get("active")
                if act:
                    lead = (now - datetime.strptime(act["at"], fmt)).total_seconds() / 60.0
                    self.db.close_warning(act["id"], "confirmed", now_s, round(lead, 1))
                    self.db.write_event("warning", f"Early warning confirmed: {label} reached {start:g} {unit}, "
                                        f"{lead:.0f} min after the warning (the sensor alone would have shown it now)",
                                        level="warning", pollutant=kind,
                                        details={"lead_min": round(lead, 1), "warned_at": act["at"]}, ts=now_s)
                    happened = "confirmed"
                else:
                    self.db.write_missed_crossing(field, kind, start, now_s)
                    self.db.write_event("warning", f"{label} reached {start:g} {unit} without an early warning "
                                        f"(the cameras did not see the cause in time)", level="warning",
                                        pollutant=kind, ts=now_s)
                    happened = "missed"
                st.update(crossed=True, active=None, seen=0)
        else:
            if value < stop:
                st["crossed"] = False
            if not st.get("crossed"):
                act = st.get("active")
                st["seen"] = (st.get("seen", 0) + 1) if ttt_continue is not None else 0
                if not act and st["seen"] >= self.CONFIRM_MINUTES:
                    wid = self.db.open_warning(field, kind, start, now_s, ttt_continue, peak_continue, peak_committed)
                    fan_txt = ""
                    if ttt_fan is None and peak_fan is not None:
                        fan_txt = f" With the fan on now it stays below (peak {peak_fan:.0f})."
                    elif ttt_fan is not None:
                        fan_txt = f" With the fan on now: in about {ttt_fan} min."
                    committed_txt = (f" Already in the air: rises to {peak_committed:.0f} even if work stops."
                                     if peak_committed is not None else "")
                    certain = ttt_committed is not None
                    head = (f"Early warning (certain, already in the air): {label} will reach {start:g} {unit} in about "
                            f"{ttt_committed} min even if work stops now." if certain else
                            f"Early warning: {label} expected to reach {start:g} {unit} in about {ttt_continue} min "
                            f"as work usually goes on (peak {peak_continue:.0f}).")
                    self.db.write_event("warning", head + ("" if certain else committed_txt) + fan_txt,
                                        level="critical" if certain else "warning", pollutant=kind,
                                        details={"ttt_min": ttt_continue, "peak": peak_continue, "certain": certain,
                                                 "committed_peak": peak_committed, "ttt_fan": ttt_fan}, ts=now_s)
                    st["active"] = {"id": wid, "at": now_s}
                    happened = "warned"
                elif act:
                    age = (now - datetime.strptime(act["at"], fmt)).total_seconds() / 60.0
                    if age > self.EXPIRE_MINUTES and ttt_continue is None:
                        self.db.close_warning(act["id"], "false alarm")
                        self.db.write_event("warning", f"Early warning for {label} withdrawn: the threshold was not "
                                            f"reached within {self.EXPIRE_MINUTES} min", level="info",
                                            pollutant=kind, ts=now_s)
                        st["active"] = None
                        happened = "false alarm"
        self.db.set_state(f"ew_{kind}", json.dumps(st))
        return happened
