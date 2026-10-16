"""SQLite storage. One writer connection (guarded by a lock); readers open their own."""
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

TABLES = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

-- Camera <-> location <-> zone mapping, refreshed from config.yaml at every start.
CREATE TABLE IF NOT EXISTS cameras (
    camera    INTEGER PRIMARY KEY,     -- NVR channel number
    name      TEXT,
    location  TEXT
);

-- Zones of the lab (config.yaml). inside = 1 counts towards "people inside the lab".
CREATE TABLE IF NOT EXISTS zones (
    zone    TEXT PRIMARY KEY,
    name    TEXT,
    inside  INTEGER,
    sort    INTEGER
);

-- One row per camera per minute. Since version 6 every count and motion value covers only
-- the persons inside the camera's detection area (roi.json). See analytics.py.
-- BH/s = body heights per second.
CREATE TABLE IF NOT EXISTS camera_minute (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    window_start   TEXT    NOT NULL,   -- local time, start of the minute
    camera         INTEGER NOT NULL,   -- NVR channel number
    camera_name    TEXT,
    location       TEXT,               -- place the camera watches (from config.yaml)
    n_person       INTEGER,            -- occupancy: maximum persons in the area in one picture
    n_person_mean  REAL,               -- occupancy: average over the window's pictures
    n_person_mode  INTEGER,            -- occupancy: typical count of the minute (most frequent per picture)
    mu_motion      REAL,               -- original H_t: mean of the frame scores (flow pixels)
    sigma2_motion  REAL,               -- original H_t: variance of the frame scores
    n_samples      INTEGER,            -- pictures analysed in the window
    unique_persons INTEGER,            -- different tracked person IDs in the area
    n_obs          INTEGER,            -- person observations in the area (persons x pictures)
    mu_person      REAL,               -- mean activity per person observation, BH/s (NULL: nobody)
    sigma2_person  REAL,               -- variance of the activity over person observations (NULL < 2)
    mu_speed       REAL,               -- mean walking speed of the tracked persons, BH/s
    n_moving_mean  REAL,               -- persons moving: average per picture
    n_moving_max   INTEGER,            -- persons moving: maximum in one picture
    motion_total   REAL,               -- total motion: mean over pictures of the summed activity, BH/s
    motion_total_var REAL,             -- temporal variance of the total motion (NULL < 2 pictures)
    motion_index   REAL,               -- 0-100 display scale of motion_total (analytics.index_ref)
    n_valid        INTEGER,            -- pictures with optical flow (used for the two above)
    n_detected_max INTEGER,            -- persons detected in the whole picture (also outside the area)
    UNIQUE (window_start, camera)
);

-- One row per tracked person per analysed picture (kept for keep_seconds_days).
CREATE TABLE IF NOT EXISTS person_obs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    Timestamp  TEXT    NOT NULL,
    camera     INTEGER NOT NULL,
    global_id  INTEGER NOT NULL,       -- camera * 1000000 + person number on that camera
    conf       REAL,                   -- YOLO confidence
    in_roi     INTEGER,                -- 1 = feet inside the detection area
    flow_px    REAL,                   -- mean optical-flow magnitude in the box (pixels)
    activity   REAL,                   -- flow / box height / flow interval, BH/s (after noise filter)
    activity_raw REAL,                 -- the same before the noise filter
    speed      REAL,                   -- feet displacement since the previous picture, BH/s (after filter)
    speed_raw  REAL,                   -- the same before the noise filter
    moving     INTEGER,
    x1 INTEGER, y1 INTEGER, x2 INTEGER, y2 INTEGER
);

-- Per zone (and '_inside' = all inside zones) per minute, pooled over person observations.
CREATE TABLE IF NOT EXISTS zone_person_minute (
    window_start    TEXT NOT NULL,
    zone            TEXT NOT NULL,
    n_obs           INTEGER,
    unique_persons  INTEGER,
    mu_person       REAL,
    sigma2_person   REAL,
    mu_speed        REAL,
    PRIMARY KEY (window_start, zone)
);

-- One row per analysed frame (kept for keep_seconds_days).
CREATE TABLE IF NOT EXISTS camera_second (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    Timestamp     TEXT    NOT NULL,
    camera        INTEGER NOT NULL,
    Person_Count  INTEGER,             -- persons in the detection area
    Motion_Score  REAL,                -- original frame score (pixels)
    n_detected    INTEGER,             -- persons in the whole picture
    n_moving      INTEGER,
    motion_total  REAL                 -- summed activity, BH/s (NULL: no optical flow)
);

-- Event log: everything worth finding again (no pictures, no names: person numbers only).
-- kind: system, camera, sensor, threshold, forecast, roi, occupancy, fan_mode, fan_command,
--       fan_decision, fan_error, controller, error
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,          -- local time
    kind       TEXT NOT NULL,
    level      TEXT,                   -- info / warning / critical
    message    TEXT,
    camera     INTEGER,
    roi        TEXT,
    pollutant  TEXT,
    fan_state  TEXT,                   -- on / off (requested) where it applies
    mode       TEXT,                   -- fan mode at the time: manual / ai
    ok         INTEGER,                -- command result
    user       TEXT,
    details    TEXT                    -- JSON: readings, forecasts, thresholds, values
);

-- Door counter: one row per person crossing the counting line.
CREATE TABLE IF NOT EXISTS door_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL,       -- local time
    camera     INTEGER NOT NULL,
    direction  TEXT    NOT NULL,       -- 'in' or 'out'
    track_id   INTEGER
);

-- Door counter per minute. inside_end = people inside the lab by door count at minute end.
CREATE TABLE IF NOT EXISTS door_minute (
    window_start  TEXT PRIMARY KEY,
    camera        INTEGER,
    n_in          INTEGER,
    n_out         INTEGER,
    inside_end    INTEGER
);

-- Ventilation context per minute (for the physics of the room air): the exhaust fan
-- state as the app commanded it. dry_run = 1: the relay was not really switched.
CREATE TABLE IF NOT EXISTS context_minute (
    window_start  TEXT PRIMARY KEY,
    fan_on        INTEGER,
    fan_signal    INTEGER,
    dry_run       INTEGER,
    mode          TEXT
);

-- Machines and enclosures (E_mach of the mass balance), pushed by a machine agent, a smart
-- plug or later the camera (boundary state): one row per machine per minute.
CREATE TABLE IF NOT EXISTS machine_minute (
    window_start  TEXT NOT NULL,
    machine       TEXT NOT NULL,       -- e.g. laser1, laser1_lid, fume_extractor
    kind          TEXT,                -- machine / enclosure / extractor / other
    on_state      INTEGER,             -- 1 running / open, 0 off / closed
    power_w       REAL,                -- electrical power if measured
    PRIMARY KEY (window_start, machine)
);

-- What the physics of the room air looked like each minute (one row per pollutant):
-- for the Physics page and for the paper.
CREATE TABLE IF NOT EXISTS physics_minute (
    window_start   TEXT NOT NULL,
    field          TEXT NOT NULL,
    value          REAL,               -- sensor value of the minute
    background     REAL,               -- C_b
    removal_per_hour REAL,             -- r used for this minute
    lambda_online_per_hour REAL,       -- CO2: live air change while occupied
    source_per_min REAL,               -- S: what was added per minute (units of the sensor)
    forecast_physics REAL,             -- value in `lead` minutes by the mass balance only
    forecast_used  REAL,               -- value in `lead` minutes by the variant in use
    used           TEXT,
    PRIMARY KEY (window_start, field)
);

-- Pollution in transit (transit.py): the newest forecast paths and model per pollutant,
-- overwritten every minute (JSON), and every early warning with its outcome.
CREATE TABLE IF NOT EXISTS transit_now (
    field      TEXT PRIMARY KEY,
    made_at    TEXT,
    data       TEXT
);
CREATE TABLE IF NOT EXISTS early_warnings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    field           TEXT,
    pollutant       TEXT,
    threshold       REAL,
    warned_at       TEXT,              -- first minute the 'continue' path reached the threshold
    ttt_min         INTEGER,           -- predicted minutes to the threshold at that moment
    predicted_peak  REAL,
    committed_peak  REAL,
    crossed_at      TEXT,              -- when the sensor really reached the threshold (NULL: not yet / never)
    lead_min        REAL,              -- crossed_at - warned_at: how much earlier than the sensor
    outcome         TEXT               -- open / confirmed / missed (crossing without warning) / false alarm
);

CREATE TABLE IF NOT EXISTS app_state (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- ThingSpeak channel entries, one row per entry.
CREATE TABLE IF NOT EXISTS sensor_readings (
    entry_id        INTEGER PRIMARY KEY,
    created_at_utc  TEXT,
    ts_local        TEXT,              -- same instant in this PC's local time
    window_start    TEXT,              -- ts_local floored to the minute (joins camera_minute)
    field1 REAL, field2 REAL, field3 REAL, field4 REAL,
    field5 REAL, field6 REAL, field7 REAL, field8 REAL
);

CREATE TABLE IF NOT EXISTS sensor_fields (
    field  TEXT PRIMARY KEY,           -- field1 .. field8
    name   TEXT
);

-- Forecasts: value predicted for `predicted_for`, made at `predicted_at`.
CREATE TABLE IF NOT EXISTS forecasts (
    predicted_for  TEXT NOT NULL,
    field          TEXT NOT NULL,      -- field1 .. field8
    predicted_at   TEXT,
    lead_minutes   INTEGER,
    value          REAL,               -- forecast shown on the dashboard
    base_value     REAL,               -- sensor value when the forecast was made ("no change")
    model_value    REAL,               -- what the model said (equals value unless it was paused)
    PRIMARY KEY (predicted_for, field)
);

-- Latest forecast model per sensor field with its test errors (mean absolute error).
CREATE TABLE IF NOT EXISTS forecast_models (
    field            TEXT PRIMARY KEY,
    trained_at       TEXT,
    n_samples        INTEGER,
    mae_persistence  REAL,             -- "no change" baseline
    mae_sensors      REAL,             -- model using the sensor's own history only
    mae_full         REAL,             -- model also using persons and motion
    used             TEXT,             -- full / sensors / no change / paused (see forecast.py)
    driver           TEXT,             -- persons / mean motion / motion variance: clearest link
    lag_minutes      INTEGER,          -- estimated travel time from that driver to the sensor
    lag_corr         REAL,             -- correlation at that delay
    live_mae         REAL,             -- error of the forecasts really issued (last 90 minutes)
    live_mae_persistence REAL,         -- error "no change" would have had on the same forecasts
    live_n           INTEGER,
    -- physics of the room air (physics.py / greybox.py), version 7.2
    mae_physics      REAL,             -- mass balance only
    mae_greybox      REAL,             -- mass balance + data residual
    mae_physreg      REAL,             -- regression with physics-informed loss
    background       REAL,             -- C_b: level the air relaxes to
    removal_per_hour REAL,             -- r = lambda (+ deposition), fan off, per hour
    removal_fan_per_hour REAL,         -- same with the exhaust fan on (NULL: not measured yet)
    n_decays         INTEGER,          -- decay periods used for r
    removal_source   TEXT,             -- decay / default
    lambda_online_per_hour REAL,       -- CO2 only: live air-change estimate while occupied
    co2_gen_lpm      REAL,             -- CO2 only: fitted generation per person, L/min (check: ~0.3)
    mae_transit      REAL              -- pollution-in-transit model (transit.py), 'continue' path
);

CREATE INDEX IF NOT EXISTS idx_cm_ws     ON camera_minute (window_start);
CREATE INDEX IF NOT EXISTS idx_cs_ts     ON camera_second (Timestamp);
CREATE INDEX IF NOT EXISTS idx_sensor_ws ON sensor_readings (window_start);
CREATE INDEX IF NOT EXISTS idx_sensor_ts ON sensor_readings (ts_local);
CREATE INDEX IF NOT EXISTS idx_door_ts   ON door_events (ts);
CREATE INDEX IF NOT EXISTS idx_pobs_ts   ON person_obs (Timestamp);
CREATE INDEX IF NOT EXISTS idx_ev_ts     ON events (ts);
CREATE INDEX IF NOT EXISTS idx_ev_kind   ON events (kind, ts);
"""

# Views are dropped and rebuilt at every start so that definition changes take effect.
VIEWS = {
    # All cameras together, per minute.
    "all_cameras_minute": """
        SELECT window_start, COUNT(*) AS cameras, SUM(n_person) AS n_person_total,
               AVG(mu_motion) AS mu_motion_mean, AVG(sigma2_motion) AS sigma2_motion_mean
        FROM camera_minute GROUP BY window_start""",
    # Per location, per minute (adds up cameras that share a location).
    "location_minute": """
        SELECT window_start, location, COUNT(*) AS cameras, SUM(n_person) AS n_person_total,
               AVG(mu_motion) AS mu_motion_mean, AVG(sigma2_motion) AS sigma2_motion_mean
        FROM camera_minute GROUP BY window_start, location""",
    # Per zone, per minute. Occupancy, moving persons and total motion are the cameras'
    # values added up (a zone's motion is the motion of all its people); the temporal
    # variance of a sum of independent cameras is the sum of their variances. Mean and
    # variance per person and speed are pooled over every person observation in the zone.
    # Motion columns are NULL for minutes stored before version 6 (those used pixels).
    "zone_minute": """
        SELECT cm.window_start, cm.zone, z.name AS zone_name, z.inside, cm.cameras,
               cm.n_person, cm.n_person_mean, cm.n_moving, cm.motion_total, cm.motion_var,
               p.unique_persons, p.n_obs, p.mu_person, p.sigma2_person, p.mu_speed,
               cm.mu_motion_legacy, cm.n_person_mode
        FROM (SELECT m.window_start, c.zone, COUNT(*) AS cameras, SUM(m.n_person) AS n_person,
                     SUM(m.n_person_mean) AS n_person_mean, SUM(m.n_moving_mean) AS n_moving,
                     SUM(m.n_person_mode) AS n_person_mode,
                     SUM(m.motion_total) AS motion_total, SUM(m.motion_total_var) AS motion_var,
                     AVG(m.mu_motion) AS mu_motion_legacy
              FROM camera_minute m JOIN cameras c ON c.camera = m.camera
              GROUP BY m.window_start, c.zone) cm
        JOIN zones z ON z.zone = cm.zone
        LEFT JOIN zone_person_minute p ON p.window_start = cm.window_start AND p.zone = cm.zone""",
    # All zones marked inside, per minute: people inside the lab as seen by the cameras.
    "inside_minute": """
        SELECT cm.window_start, cm.cameras, cm.n_person, cm.n_person_mean, cm.n_moving,
               cm.motion_total, cm.motion_var, p.unique_persons, p.n_obs, p.mu_person,
               p.sigma2_person, p.mu_speed, cm.mu_motion_legacy, cm.n_person_mode
        FROM (SELECT m.window_start, COUNT(*) AS cameras, SUM(m.n_person) AS n_person,
                     SUM(m.n_person_mean) AS n_person_mean, SUM(m.n_moving_mean) AS n_moving,
                     SUM(m.n_person_mode) AS n_person_mode,
                     SUM(m.motion_total) AS motion_total, SUM(m.motion_total_var) AS motion_var,
                     AVG(m.mu_motion) AS mu_motion_legacy
              FROM camera_minute m JOIN cameras c ON c.camera = m.camera
                                   JOIN zones z ON z.zone = c.zone
              WHERE z.inside = 1 GROUP BY m.window_start) cm
        LEFT JOIN zone_person_minute p ON p.window_start = cm.window_start AND p.zone = '_inside'""",
    # Sensor averages per minute.
    "sensor_minute": """
        SELECT window_start, COUNT(*) AS n_entries,
               AVG(field1) AS field1, AVG(field2) AS field2, AVG(field3) AS field3,
               AVG(field4) AS field4, AVG(field5) AS field5, AVG(field6) AS field6,
               AVG(field7) AS field7, AVG(field8) AS field8
        FROM sensor_readings GROUP BY window_start""",
}

FIELDS = [f"field{i}" for i in range(1, 9)]


class Database:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._con = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        # zone_person_minute of an unreleased test build had other columns: it is derived
        # data, so it is simply made again
        cols = [r[1] for r in self._con.execute("PRAGMA table_info(zone_person_minute)")]
        if cols and "mu_person" not in cols:
            self._con.execute("DROP TABLE zone_person_minute")
        self._con.executescript(TABLES)
        # databases made by earlier versions get the newer columns
        for table, column, kind in (("cameras", "zone", "TEXT"),
                                    *[("camera_minute", c, k) for c, k in (
                                        ("unique_persons", "INTEGER"), ("n_obs", "INTEGER"),
                                        ("mu_person", "REAL"), ("sigma2_person", "REAL"),
                                        ("mu_speed", "REAL"), ("n_moving_mean", "REAL"),
                                        ("n_moving_max", "INTEGER"), ("motion_total", "REAL"),
                                        ("motion_total_var", "REAL"), ("motion_index", "REAL"),
                                        ("n_valid", "INTEGER"), ("n_detected_max", "INTEGER"),
                                        ("n_person_mode", "INTEGER"))],
                                    ("camera_second", "n_detected", "INTEGER"),
                                    *[("forecast_models", c, k) for c, k in (
                                        ("mae_physics", "REAL"), ("mae_greybox", "REAL"), ("mae_physreg", "REAL"),
                                        ("background", "REAL"), ("removal_per_hour", "REAL"),
                                        ("removal_fan_per_hour", "REAL"), ("n_decays", "INTEGER"),
                                        ("removal_source", "TEXT"), ("lambda_online_per_hour", "REAL"),
                                        ("co2_gen_lpm", "REAL"), ("mae_transit", "REAL"))],
                                    ("camera_second", "n_moving", "INTEGER"),
                                    ("camera_second", "motion_total", "REAL"),
                                    *[("person_obs", c, k) for c, k in (
                                        ("conf", "REAL"), ("in_roi", "INTEGER"), ("flow_px", "REAL"),
                                        ("activity", "REAL"), ("moving", "INTEGER"),
                                        ("activity_raw", "REAL"), ("speed_raw", "REAL"))]):
            columns = [r[1] for r in self._con.execute(f"PRAGMA table_info({table})")]
            if column not in columns:
                self._con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        for name, body in VIEWS.items():
            self._con.execute(f"DROP VIEW IF EXISTS {name}")
            self._con.execute(f"CREATE VIEW {name} AS {body}")
        self._con.commit()

    def _many(self, sql, rows):
        rows = list(rows)
        if not rows:
            return 0
        with self._lock:
            before = self._con.total_changes
            self._con.executemany(sql, rows)
            self._con.commit()
            return self._con.total_changes - before

    # ---- cameras and zones ---------------------------------------------
    def write_layout(self, cameras, zones):
        """cameras: (id, name, location, zone key); zones: dicts from config.build_zones."""
        with self._lock:
            self._con.executemany(
                "INSERT INTO cameras (camera, name, location, zone) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(camera) DO UPDATE SET name = excluded.name, "
                "location = excluded.location, zone = excluded.zone", list(cameras))
            self._con.execute("DELETE FROM zones")
            self._con.executemany(
                "INSERT INTO zones (zone, name, inside, sort) VALUES (?, ?, ?, ?)",
                [(z["key"], z["name"], int(z["inside"]), z["sort"]) for z in zones])
            self._con.commit()

    # ---- camera features -----------------------------------------------
    def write_seconds(self, rows):
        """rows: (Timestamp, camera, persons in area, frame score, detected, moving, total motion)"""
        self._many("INSERT INTO camera_second (Timestamp, camera, Person_Count, Motion_Score, "
                   "n_detected, n_moving, motion_total) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    MINUTE_COLUMNS = ("n_person", "n_person_mean", "mu_motion", "sigma2_motion", "n_samples",
                      "unique_persons", "n_obs", "mu_person", "sigma2_person", "mu_speed",
                      "n_moving_mean", "n_moving_max", "motion_total", "motion_total_var",
                      "motion_index", "n_valid", "n_detected_max", "n_person_mode")

    def write_minutes(self, rows):
        cols = ("window_start", "camera", "camera_name", "location") + self.MINUTE_COLUMNS
        self._many(f"INSERT OR REPLACE INTO camera_minute ({', '.join(cols)}) "
                   f"VALUES ({', '.join(':' + c for c in cols)})",
                   [{**{c: None for c in cols}, **r} for r in rows])

    def write_person_obs(self, rows):
        """rows: (Timestamp, camera, global_id, conf, in_roi, flow_px, activity, speed, moving,
        x1, y1, x2, y2, activity_raw, speed_raw)"""
        self._many("INSERT INTO person_obs (Timestamp, camera, global_id, conf, in_roi, flow_px, "
                   "activity, speed, moving, x1, y1, x2, y2, activity_raw, speed_raw) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def write_zone_person_minutes(self, rows):
        self._many("INSERT OR REPLACE INTO zone_person_minute (window_start, zone, n_obs, unique_persons, "
                   "mu_person, sigma2_person, mu_speed) VALUES (:window_start, :zone, :n_obs, "
                   ":unique_persons, :mu_person, :sigma2_person, :mu_speed)", rows)

    # ---- event log -----------------------------------------------------
    def write_event(self, kind, message, level="info", camera=None, roi=None, pollutant=None,
                    fan_state=None, mode=None, ok=None, user=None, details=None, ts=None):
        import json
        ts = ts or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            self._many("INSERT INTO events (ts, kind, level, message, camera, roi, pollutant, fan_state, "
                       "mode, ok, user, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       [(ts, kind, level, str(message)[:500], camera, roi, pollutant, fan_state, mode,
                         None if ok is None else int(bool(ok)), user,
                         json.dumps(details, default=str) if details is not None else None)])
        except sqlite3.Error as exc:            # the log must never stop the app
            print(f"[events] not stored: {exc}")

    def prune_seconds(self, keep_days):
        if not keep_days:
            return
        cutoff = (datetime.now() - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self._con.execute("DELETE FROM camera_second WHERE Timestamp < ?", (cutoff,))
            self._con.execute("DELETE FROM person_obs WHERE Timestamp < ?", (cutoff,))
            self._con.commit()

    # ---- door counter --------------------------------------------------
    def write_door_event(self, ts, camera, direction, track_id):
        self._many("INSERT INTO door_events (ts, camera, direction, track_id) VALUES (?, ?, ?, ?)",
                   [(ts, camera, direction, track_id)])

    def write_door_minute(self, window_start, camera, n_in, n_out, inside_end):
        self._many("INSERT OR REPLACE INTO door_minute (window_start, camera, n_in, n_out, "
                   "inside_end) VALUES (?, ?, ?, ?, ?)",
                   [(window_start, camera, n_in, n_out, inside_end)])

    def door_counts_since(self, ts):
        with self._lock:
            rows = dict(self._con.execute(
                "SELECT direction, COUNT(*) FROM door_events WHERE ts >= ? GROUP BY direction",
                (ts,)).fetchall())
        return rows.get("in", 0), rows.get("out", 0)

    def get_state(self, key, default=None):
        with self._lock:
            row = self._con.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key, value):
        self._many("INSERT INTO app_state (key, value) VALUES (?, ?) "
                   "ON CONFLICT(key) DO UPDATE SET value = excluded.value", [(key, str(value))])

    # ---- sensors -------------------------------------------------------
    def write_sensor_entries(self, rows):
        """rows: dicts with entry_id, created_at_utc, ts_local, window_start, field1..8.
        Returns the number of new rows."""
        return self._many(
            "INSERT OR IGNORE INTO sensor_readings (entry_id, created_at_utc, ts_local, "
            "window_start, " + ", ".join(FIELDS) + ") VALUES (:entry_id, :created_at_utc, "
            ":ts_local, :window_start, " + ", ".join(":" + f for f in FIELDS) + ")", rows)

    def write_sensor_fields(self, names):
        self._many("INSERT INTO sensor_fields (field, name) VALUES (?, ?) "
                   "ON CONFLICT(field) DO UPDATE SET name = excluded.name", names.items())

    # ---- forecasts -----------------------------------------------------
    def write_forecasts(self, rows):
        self._many("INSERT OR REPLACE INTO forecasts (predicted_for, field, predicted_at, "
                   "lead_minutes, value, base_value, model_value) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    MODEL_COLUMNS = ("field", "trained_at", "n_samples", "mae_persistence", "mae_sensors", "mae_full", "used",
                     "driver", "lag_minutes", "lag_corr", "live_mae", "live_mae_persistence", "live_n",
                     "mae_physics", "mae_greybox", "mae_physreg", "background", "removal_per_hour",
                     "removal_fan_per_hour", "n_decays", "removal_source", "lambda_online_per_hour",
                     "co2_gen_lpm", "mae_transit")

    def write_forecast_models(self, rows):
        """rows: tuples in MODEL_COLUMNS order (shorter tuples: the rest NULL)."""
        n = len(self.MODEL_COLUMNS)
        rows = [tuple(r) + (None,) * (n - len(r)) for r in rows]
        self._many(f"INSERT OR REPLACE INTO forecast_models ({', '.join(self.MODEL_COLUMNS)}) "
                   f"VALUES ({', '.join('?' * n)})", rows)

    def write_machine_minute(self, rows):
        """rows: (window_start, machine, kind, on_state, power_w)"""
        self._many("INSERT OR REPLACE INTO machine_minute (window_start, machine, kind, on_state, power_w) "
                   "VALUES (?, ?, ?, ?, ?)", rows)

    def write_physics_minute(self, rows):
        """rows: (window_start, field, value, background, removal_per_hour, lambda_online_per_hour,
        source_per_min, forecast_physics, forecast_used, used)"""
        self._many("INSERT OR REPLACE INTO physics_minute (window_start, field, value, background, "
                   "removal_per_hour, lambda_online_per_hour, source_per_min, forecast_physics, "
                   "forecast_used, used) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def write_transit_now(self, field, made_at, data):
        import json
        self._many("INSERT OR REPLACE INTO transit_now (field, made_at, data) VALUES (?, ?, ?)",
                   [(field, made_at, json.dumps(data, default=float))])

    def open_warning(self, field, pollutant, threshold, warned_at, ttt, peak, committed_peak):
        with self._lock:
            cur = self._con.execute(
                "INSERT INTO early_warnings (field, pollutant, threshold, warned_at, ttt_min, predicted_peak, "
                "committed_peak, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, 'open')",
                (field, pollutant, threshold, warned_at, ttt, peak, committed_peak))
            self._con.commit()
            return cur.lastrowid

    def close_warning(self, wid, outcome, crossed_at=None, lead_min=None):
        self._many("UPDATE early_warnings SET outcome = ?, crossed_at = ?, lead_min = ? WHERE id = ?",
                   [(outcome, crossed_at, lead_min, wid)])

    def write_missed_crossing(self, field, pollutant, threshold, crossed_at):
        self._many("INSERT INTO early_warnings (field, pollutant, threshold, crossed_at, lead_min, outcome) "
                   "VALUES (?, ?, ?, ?, 0, 'missed')", [(field, pollutant, threshold, crossed_at)])

    def write_context_minute(self, window_start, fan_on, fan_signal, dry_run, mode):
        self._many("INSERT OR REPLACE INTO context_minute (window_start, fan_on, fan_signal, dry_run, mode) "
                   "VALUES (?, ?, ?, ?, ?)", [(window_start, fan_on, fan_signal, dry_run, mode)])

    def close(self):
        with self._lock:
            self._con.commit()
            self._con.close()


def read_connection(path):
    """Read-only connection for the dashboard (never blocks the writer in WAL mode)."""
    con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con
