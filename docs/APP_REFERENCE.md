> Reference of the app's features, pages, settings and calibration, copied from the PC app (`CCTV_Yolo` README, v8.4). Install and run steps here are for the Windows PC; on the Raspberry Pi follow `docs/RPI5_SETUP.md`. The fan on this system is the ESP32 only (`fan: controller: esp32`).

# Lab Vision-IAQ: occupancy, motion, air quality and AI exhaust-fan control

CCTV-based person detection and tracking (YOLO26), motion analytics per detection area,
ThingSpeak air-quality sensors, a 10-minute pollutant forecast, and Manual / AI control of
an exhaust fan through a Raspberry Pi or an ESP32 — all on the lab network, nothing in the cloud.

![Fan control](docs/fan_page.png)

(version 6)

One command reads the NVR's RTSP streams, detects persons with YOLO26, follows every
person with an ID, counts and measures motion only inside each camera's detection areas,
counts people through the lab door, stores everything per minute in SQLite, stores the
ThingSpeak sensor readings, forecasts the sensors 10 minutes ahead, controls the exhaust
fan (Manual or AI) through a Raspberry Pi, and serves a dashboard to the lab network.
Nothing is changed on the NVR or the cameras.

## Install and run (Windows PC)

    conda activate cctv_yolo
    cd /d D:\AI\claude\CCTV_Yolo
    pip install -r requirements.txt        (first time, and after an update)
    set NVR_PASSWORD=your_password
    set FAN_AGENT_TOKEN=token_from_the_pi   (only when the Raspberry Pi fan agent is used)
    python run.py

Stop with Ctrl+C. Accounts: `python run.py --add-user NAME` (password typed on the PC,
only a hash is stored), `--remove-user NAME`, `--list-users`.

Other commands: `--test 8` (one frame from camera 8), `--cameras 8 3`, `--no-thingspeak`,
`--no-door`, `--calibrate` (distribution of the stored motion values, see Calibration).

## Open it from other PCs and phones on the lab network

`dashboard: host: 0.0.0.0` makes the app listen on all network cards. At start it prints

    Dashboard: http://127.0.0.1:8001
      from other PCs on the network: http://10.185.101.57:8001

Find the address yourself: Windows `ipconfig` (IPv4 Address of the lab adapter), Linux
`hostname -I`. If another device cannot open the page, Windows Firewall blocks it: click
"Allow access" on the prompt Windows shows the first time, or in an administrator
Command Prompt:

    netsh advfirewall firewall add rule name="Lab dashboard 8001" dir=in action=allow protocol=TCP localport=8001

The page only uses relative addresses, so pictures, live values, the fan and the area
editor work from any device. Everyone must sign in. Switching the fan and editing
detection areas is limited to the user names under `fan: operators:`. The connection is
plain HTTP (not encrypted): use it on the lab network only, never expose port 8001 to the
internet.

## Camera setup first, then detection

A camera is analysed only after its setup is saved. Until then it shows "SET UP FIRST",
YOLO does not look at it and nothing of it is stored or counted. Start the app, sign in
as an operator (`fan: operators:` in config.yaml), open **Setup** and for each camera:

1. click the camera;
2. choose **Area** and draw the floor area where people should be detected (several
   areas allowed), or **Whole picture**, or **Off** (camera not analysed);
3. **Save**: detection for that camera starts with the next picture.

The dashboard counts only persons whose feet are inside an area: occupancy, moving,
motion, variance, forecast and fan all use only these persons. YOLO itself looks only at
the part of the picture around the areas (dashed "YOLO" rectangle: the areas plus
`crop_margin`, and `head_room` above them so the body of a person standing at the back of
an area is seen). Persons in that rectangle but outside the areas keep their ID and are
drawn grey; they never count.

Change an area any time on the same page (Save applies it immediately). Every save,
change and Reset is written to the event log with the user and the new areas, so a
change in the numbers can be traced to a changed area. `setup: require_setup: false` in
config.yaml restores the old behaviour (a camera without setup counts the whole picture);
`detect_in: picture` lets YOLO look at the whole picture again.

### Own (custom-trained) YOLO model

Put the weights next to config.yaml and set in config.yaml:

    model:
      weights: my_lab_person.pt
      classes: [person]      # the class name(s) of persons in YOUR model, e.g. [student, staff]

The app reads the class list from the model at start and prints which class ids it uses;
a model with a single class is used as it is; if no name matches it stops and lists the
model's classes. The same works for the door counter (`door: weights:`, `door: classes:`).

## Stable counts (people sitting and working)

Made for areas where people mostly sit and work and sometimes walk:

- **One picture per second per camera** (`pipeline: tick_seconds: 1`). The per-second
  counts are stored (`camera_second`) and every minute gets the typical count of its 60
  pictures (`n_person_mode`, column "Count" on the Cameras page), the maximum and the mean.
  If the PC cannot keep up, the Overview shows how often each camera is really analysed;
  a GPU, `yolo26n`, a smaller `imgsz` or fewer areas help.
- **Missed for a moment, still counted.** A person who was sitting or standing still in
  the area and is not found in a picture stays counted with the same ID for 90 s
  (`hold_seconds`), drawn as a blue dashed box "⏸ 12s". Someone who was walking when lost
  is held only 5 s, someone at the edge of the area 30 s.
- **Same ID while in the area.** A missed person is searched where they sat (not where they
  might have walked to), and recognised by colours for 120 s.
- **People close together keep their own IDs.** Two boxes on one person (body and a part
  of it) are merged; two people side by side keep their boxes; while boxes overlap only
  the position is updated, not the colour signature.
- **No flicker from false boxes.** A new person counts after 2 pictures (at once with
  confidence ≥ 0.5).
- **People far from the camera.** Each detection area is cut out and enlarged to the
  model size (`setup: crop_per_area`), so a small far area is seen as large; and a weaker
  box (down to confidence 0.10, `tracking: conf_low`) keeps a known far person instead of
  losing them. Tip: draw the far part of a room as its own area.

## Dashboard pages

1. **Overview** — alerts, people present, moving now, motion index, temporal variance,
   fan mode / requested / relay signal, cameras, sensor freshness, system; pollutant tiles
   with forecast; pollutant chart with occupancy, moving, motion and variance below; fan
   panel; zones; recent events; all cameras small.
2. **Cameras** — the selected camera large and all cameras small, with boxes, IDs,
   confidence and detection areas drawn by the browser (switch each on/off); persons in
   the area, detected, moving, motion index, 60-s temporal variance, last processed time;
   the people table; the camera's history; the lab door card; per-minute table; CSV.
3. **Setup** — set up every camera first (see "Camera setup" below). Progress bar,
   all cameras with their state, and the editor: Area / Whole picture / Off, "+ New area",
   click the corners, "Finish", name it, drag corners, enable/disable, delete, Save, Reset.
4. **Motion & occupancy** — occupancy, moving, total motion, motion index, temporal
   variance, activity per person, between-person variance, walking speed over time for the
   lab, a zone or a camera; where people stand (heat map); per-person table; operator
   diagnostics: raw and filtered values per person and picture and a recomputation of each
   stored minute from the stored pictures (✓ = backend and display agree).
5. **Air quality** — per pollutant: reading, unit, time, Fresh/Stale, category, fan
   thresholds, alert/recovery state, 10-min forecast (only when its model beats "no
   change"); chart of measured and forecast; timeline of people, pollutant and fan events;
   forecast error.
6. **Exhaust fan** — Manual / AI, ON / OFF, requested state, relay signal read back from the
   GPIO pin, fan running (only with a feedback input), controller, GPIO pin, last command,
   reason, the AI decision with the readings and thresholds it used, the policy, fan events.
7. **Events** — every event with filters (time range above, type, level, camera, area,
   pollutant, fan state, text) and CSV download.

Missing data is shown as "Unavailable", old sensor data as "Stale", never as 0.

## Motion and variance: what was wrong and what is measured now

What made the old variance low with many people (all checked on the stored data and with
`tests/validate_motion.py`):

1. **Optical flow over a whole second.** The flow was measured between frames 1 s apart.
   Farneback cannot follow a walking person that far: measured on the synthetic camera, a
   person walking 0.5 body heights/s read as 5 % of the true motion, slow movement as 96 %.
   Walking people looked almost still. Now the two frames are 0.12 s apart
   (`pipeline.flow_interval`): about 90-97 % of the true motion up to about 1 body height/s for near persons (less for very small, far ones).
2. **The frame score is a mean over all boxes.** With N people its picture-to-picture
   variation shrinks roughly like 1/N, so a full room gave a smaller variance.
3. **2-3 pictures per minute** with the large model: hardly any samples for a variance.
   yolo26s at 960 px gives about 12.
4. **Zones and the whole lab were averages of cameras, including empty ones.**
5. **Pixels.** A person far away moves fewer pixels than one close to the camera; cameras of
   different resolution gave different values.
6. **Persons outside the lab** (far away, through glass) were counted.

Now, per camera, only persons whose feet are inside an enabled detection area count:

| Measure | Definition |
|---|---|
| Occupancy | persons in the area per picture (one tracked ID per box); per minute max and mean; different IDs |
| Activity (per person) | optical flow in the box ÷ box height ÷ time between the two frames = body heights per second (BH/s); below `activity_noise` = 0 |
| Walking speed (per person) | feet displacement since the person's previous picture ÷ height ÷ time (BH/s); below `speed_noise` = 0; unknown for the first picture |
| Moving | activity or speed above its noise threshold |
| Total motion | sum of the activity of the persons in the area (BH/s); 0 for an empty area; zone and lab = sum of their cameras |
| Motion index | 100 × (1 − e^(−total / `index_ref`)): 0 nobody moves, 63 = index_ref, 95 = 3 × index_ref |
| Temporal variance | mean squared deviation of the total motion of the minute's pictures from their mean (population variance); NULL with fewer than 2 pictures; zone = sum of the cameras' variances |
| Between-person variance | variance of the activity over all person observations of the minute (NULL below 2) |

The original H<sub>t</sub> of iaq-edge-pipeline (`mu_motion`, `sigma2_motion`, pixels) is
still stored per camera for comparison. The forecast now uses persons, total motion, its
temporal variance and speed; each joins the model once there are `forecast.min_samples`
minutes of it.

Validation: `python tests/validate_motion.py` runs empty room, one/five still persons, one
and four walkers, ten present with two walking, entering/leaving the area, occlusion,
a walker outside the area, the same scene at two resolutions, and constant versus
stop-and-go walking, through the real flow, tracker, areas and statistics, and checks the
expected behaviour. `python -m pytest tests` runs all tests (33).

## Person IDs

Each camera follows its persons from picture to picture: expected position from the last
movement, distance of the feet, colours of upper and lower body, best overall pairing.
While two people overlap their colours and velocity are not updated, so they keep their
IDs after crossing. A person out of view for up to `tracking.reid_seconds` (90 s) is
recognised by colours and keeps their ID. IDs are per camera
(global_id = camera × 1,000,000 + number): the same person seen by two cameras has two
IDs; there is no cross-camera identity. Tracking runs on the whole picture; the areas
only decide who counts.

## Physics-informed and grey-box forecast

The room air is written as a single-zone mass balance (EAMTP plan, sections 5.1-5.2):

    dC/dt = S_t - r_t (C - C_b)

| term | meaning | how the app gets it |
|---|---|---|
| C_b | level the air relaxes to (outdoor / empty room) | low percentile of the history (CO2 at least 400 ppm) until an outdoor sensor exists |
| r_t | removal rate = air change λ (+ deposition for particles) | decay periods of the empty room: ln(C - C_b) falls with slope -r (tracer-gas decay), separately with the exhaust fan on and off; for CO2 also live while occupied |
| λ_t (CO2) | live air change while people are present | rolling one-hour fit of dC/dt = a·N_t + b·M_t - λ_t (C - C_b) with the camera head count N_t and motion M_t |
| G | CO2 per person = a · V | a physical check of the camera count: about 0.3 L/min for seated adults |
| S_t | source: what people, motion and machines add | regression of the source each minute must have had on what the cameras saw in the past 30 minutes (with their delays) |

Every minute, for every pollutant, five forecasts of the change over the next 10 minutes
are compared on the same test minutes (last quarter of the history, embargo gap of
40 minutes so overlapping windows cannot leak):

1. **No change** (persistence): the floor.
2. **Physics**: the mass balance integrated 10 minutes ahead (closed form).
   The learned parts below see the recent changes, the camera drivers and the context,
   never the absolute level (that stays with the physics).
3. **Data**: ridge regression on the recent readings and the camera drivers (version 6).
4. **Grey-box**: physics + a regression that learns only the residual, with the
   ventilation context (fan state, removal rate, door traffic) as input.
5. **Physics-informed loss**: regression trained to agree with the measured change *and*
   with the mass balance (`physics_weight`).

The best one that beats "no change" is used; the Air page shows which, the removal rates,
the background and the live air change in the forecast note. The fan state is recorded
every minute in `context_minute`; only minutes where the relay was really switched (not
dry run) count as "fan on".

**Set the room volume** in config.yaml (`physics: volume_m3`). λ does not depend on it,
but the CO2 generation per person and the emission terms do.

### Dashboard page "Physics" and the overview

The **Physics** page shows the equation with its three terms, the inputs of each term
(live / dry run / estimated / future), one card per pollutant (background, removal rate
with the fan off and on, decay periods, live air change, CO₂ per person, and the skill of
the four forecasts on the same test minutes; the variant in use is highlighted), a time
chart (measured value with the physics forecast and the forecast in use drawn at the
time they were meant for, removal rate and live λ, source S) and the research
evaluation table once `research\greybox_eval.py` has been run. The **Overview** has a
"Room air physics" card with the key points: air change, the forecast in use with its
skill, live λ and CO₂ per person.

Sensors listed under `dashboard: hide_fields` (default `[rawvoc]`) are left out of every
page; they are still stored and in the CSV export.

### Physics and model events

The forecaster writes events (Events page types "Physics" and "Forecast model", also
listed on the Physics page and the latest one on the Overview card) only when something
changes; the last state per pollutant is kept, so a restart repeats nothing:

| Type | Written when | Level |
|---|---|---|
| Forecast model | the variant in use changes (physics, grey-box, physics-informed, data, no change), with its skill | info |
| Forecast model | the forecast is paused (worse than "no change") / resumes | warning / info |
| Physics | a removal rate is measured from empty-room decays (fan off), or changes by more than 20 % | info |
| Physics | the removal with the fan on is measured; warning when it is under 1.3 x the fan-off value | info / warning |
| Physics | live air change below half the usual value for 10 occupied minutes (room sealed?) and when it recovers | warning / info |
| Physics | at the current activity the level heads above the fan start threshold (C_b + S/r) for 3 minutes, before the reading gets there | warning |
| Physics | the pollutant rises with nobody in the detection areas for 3 minutes (machine, door, outside air) | warning |
| Physics | CO2 per person from the camera count outside 0.15-0.6 L/min, and back | warning / info |

### Early warning: pollution in transit (version 8)

The sensor is late: dust from a far bench needs minutes to reach it. The page **Early
warning** predicts the sensor before it records the rise (`iaqcam/transit.py`):

1. **Every inside camera is a source.** Its persons, total motion and motion variance per
   minute, averaged over lag blocks (0-2, 2-5, 5-10, 10-20, 20-30 min ago).
2. **Physics-supervised emission and transport.** The source the sensor must have received,
   s = dC/dt + r(C - C_b), is regressed on those blocks with weights that can never be
   negative (non-negative ridge). The weights of a camera ARE its transport kernel: their
   centre is the travel time to the sensor; their sum per activity is an emission factor
   (x room volume: µg/min for particles, mL/min for CO2). No labels are needed.
3. **Pollution in transit.** At every minute the arriving source for the next 30 minutes is
   split into what was emitted before now (already in the air) and what still depends on
   future activity. Four paths are integrated exactly with the mass balance:
   - *committed*: everyone stops now; certain, it is already on its way;
   - *expected*: each camera's activity relaxes to its usual level with its learned
     persistence (AR(1) per camera and activity);
   - *worst case*: activity stays as now;
   - *expected, fan on now*: with the measured fan-on removal rate.
4. **Two-tier early warning.** Committed path reaching the fan start threshold = certain
   (critical); expected path = likely (warning). Each warning is stored in
   `early_warnings` and closed as *confirmed* (with the lead time over the sensor),
   *missed* or *false alarm*; events of type "Early warning" go to the event log; the
   Overview shows them as alerts.

Simulation (tests/sim_zones.py, 30 days, train 10 / test 20): travel times recovered
(near zone 2.0 vs 1.5 min true, far zone 9.2 vs 9.0); 10-min error 0.55 vs 1.69 for "no
change" and 1.62 for the well-mixed physics; every threshold crossing warned, median
12-13 min before the sensor, with fewer false alarms than extrapolating the sensor's
trend or the well-mixed physics; the committed tier never gave a false alarm. Real lab
data decide. `python research\greybox_eval.py --demo-zones` repeats it; on your data the
summary gets an "Early warning" table per pollutant with a fan threshold.

### Machines and enclosures (E_mach): ready for the next study

The mass balance has a machine source term E_mach(B_t). Right now there is no machine
data, so the Physics page lists "Machines / enclosures" as future. The product is ready
for it:

- table `machine_minute` (minute, machine, kind machine/enclosure/extractor, on/off, power W);
- `POST /api/machines` (operator sign-in, or `Authorization: Bearer <MACHINE_AGENT_TOKEN>`
  for agents), `GET /api/machines`;
- `machine_agent\report_machine.py`: one command per state change from the laser PC, a
  smart plug reader or a lid switch on a Raspberry Pi;
- once 180 minutes are recorded, "machines on" joins the forecast automatically as a
  source driver (emission model) and as context, in the app and in the research script.

The same pattern takes the other future inputs of the research plan (windows from the
camera's boundary detection, outdoor air, job logs with material and laser power):
one table per input, one ingestion endpoint, one term of the mass balance.

### Research evaluation (for the paper)

    python research\greybox_eval.py --fields co2 pm2_5 pm10 --epochs 30
    python research\greybox_eval.py --demo             (simulated lab days: checks the pipeline)

Reads the database and writes `research_out/` with `summary.md` (MAE, RMSE, skill vs
persistence per split and model), `metrics.csv` (every fold), `physics.json` (λ, removal
rates, background, CO2 per person) and `dataset.csv` (one row per minute). Splits as in
plan section 6: rung0 chronological, rung1 leave-one-day-out, rung2 leave-condition-out
(fan on never seen in training; its λ comes only from empty-room decay tests), rung4
forward chaining; constants are estimated on the training rows of each fold. Models:
persistence, physics, ridge, ridge grey-box, ridge with physics loss, and with PyTorch
(installed with ultralytics) BiLSTM, grey-box BiLSTM residual, BiLSTM with the
mass-balance penalty in the loss (PINN) and grey-box FiLM-BiLSTM conditioned on the
context. A full run on several weeks takes a while on CPU; `--max-folds 3` limits it.

On simulated lab days with a known mass balance the code recovers the air change (fan off
1.47 vs true 1.5 per hour, fan on 6.1 vs 6.0) and the CO2 per person (0.29 vs 0.30 L/min).
With a ventilation state never seen in training (rung2), the data-only regression fell far
below "no change" (CO2 MAE 83 ppm vs 28 ppm, PM2.5 6.9 vs 2.4 µg/m³), while the physics
forecast (12 ppm, 1.3) and the grey-box (12.8 ppm, 0.93) stayed well above it. One design
rule came out of this: the learned part of the grey-box gets the recent *changes*, not the
absolute level - with the level it fought the physics under the new ventilation (PM2.5
MAE 2.3 instead of 0.93). These are simulation results; the real lab data decide.

**Paper figures (v8.1).** After the evaluation:

    python research\make_figures.py research_out

writes `research_out/figures/` (PNG 300 dpi + PDF): fig1 a typical day, fig2 empty-room
decays (removal rate), fig3 source vs persons (CO2 L/min per person), fig4 skill per model
and split, fig5 transit kernels per camera, fig6 one worked early warning, fig7 lead time /
false alarms per method. A figure is skipped when its input does not exist yet.

**Experiment log (v8.1).** Events page, top card: an operator picks the test type (empty-room
decay fan off / on, CO2 per person, particle source near / far, fan step, camera count
check, baseline day), writes a note and presses Start, then Stop. Stored as events of kind
`experiment`; `greybox_eval.py` exports them to `research_out/experiments.csv`, so every
test in the paper has its exact minutes.

## Exhaust fan

**Hardware path.** Dashboard → this app (Manual/AI, one lock, so commands never conflict)
→ HTTP with a secret token → `pi_fan_agent.py` on the Raspberry Pi 4 → GPIO 9 (BCM, header
pin 21) → relay module → contactor/relay rated for the fan. The agent is the only program
that uses the pin.

**Safety.** Never connect a mains fan to a GPIO pin: use a properly rated relay or contactor
with isolation, installed by a qualified electrician. The app reads back the GPIO output
level ("relay signal"); that proves the control signal, not that the fan turns. Wire a
feedback contact or current sensor to `--feedback-pin` to see "fan running". This system
does not replace required ventilation, exposure controls or hardware interlocks.

**Raspberry Pi setup** (folder `pi_fan_agent`: `pi_fan_agent.py`, `fan_controller.py`,
`relay_test.py`, `pi-fan-agent.service`):

1. Copy the folder to the Pi, e.g. `/home/pi/pi_fan_agent`.
2. With the fan NOT connected, run `python3 relay_test.py`: the relay must click on with
   HIGH and off with LOW. If it is the other way round, the module is active-LOW: start the
   agent with `--active-low` (nothing else changes).
3. `python3 pi_fan_agent.py --mock` first (no GPIO), then `python3 pi_fan_agent.py`. The
   first start prints a token and saves it in `fan_agent_token.txt`.
4. Limit access to the lab PC: `--allow 10.185.101.57` (the PC's address).
5. Start at boot: edit and install `pi-fan-agent.service` (instructions inside).

**App setup** (`config.yaml`, `fan:`): `operators: [your_user]`; first keep
`dry_run: true` (commands are simulated, shown as DRY RUN everywhere) and try Manual and
AI. Then `dry_run: false`, `controller: remote`, `remote: url: http://<pi-address>:8095`,
and `set FAN_AGENT_TOKEN=...` before `python run.py`. `controller: gpio` is for running the
whole app on the Pi.

**Manual mode**: ON / OFF buttons only; the AI makes no decisions. A second press while a
command is running is refused. The result says "accepted by the controller"; the relay
signal is read back from the pin.

**AI mode** (`fan: ai:`):

- Start when a reading stays ≥ `start` for `activation_seconds`, or when
  `forecast_confirmations` forecasts in a row are ≥ `start`, only from a model that beats
  "no change" (`forecast_requires_model`), and not within `min_off_seconds` of the last stop.
- Stop only when *all* controlled pollutants are ≤ `stop` (below `start`: hysteresis) for
  `recovery_seconds` of measured readings, after `min_on_seconds`, and no forecast trigger
  is active. A falling forecast never stops the fan.
- Every decision, reading, forecast and threshold is in the event log.
- Mode changes: Manual → AI decides at once from the current data and fan state; AI →
  Manual stops AI commands, the fan keeps its state until a button is pressed.
- Restart: the mode is kept; Manual re-sends the last manual state; AI starts from the
  state the Pi reports.

**Fail-safe** (lab decision): stale sensor data (older than `stale_seconds`) in AI mode →
fan OFF (`stale_action: "off"`). The Pi agent switches the fan OFF if the PC stops talking
to it for 120 s (`--on-comm-loss off`). If this fan is essential ventilation, use
`stale_action: "on"` and `--on-comm-loss on` instead.

The thresholds in `config.yaml` are starting values taken from the dashboard's category
bands. They are not verified for this lab.

## Raspberry Pi and ESP32 as alternatives (v8.3)

`fan: controller: dual` uses both boards. Only the **active** one switches the fan; the other
is on **standby** and is kept OFF, so it can never hold the fan on. The Fan page shows
`Raspberry Pi | ESP32` with a green / red dot each: click one to move control (operators).
With `failover: auto`, a command or heartbeat that fails on the active board moves control
to the standby board, which repeats the last requested state; the move is logged as a
warning event. Each board switches its own relay OFF after 120 s without contact.

Wiring: two SSRs with their outputs **in parallel** on the fan's live wire (the fan runs
when either is ON), one driven by the Pi (GPIO 9), one by the ESP32 (GPIO 26, 10 kΩ to GND).
Never connect two controller outputs to the same SSR input.

Tokens: Pi = `fan_agent_token.txt`, ESP32 = `esp32_agent_token.txt` (both next to
config.yaml, never uploaded). ESP32 Wi-Fi and token: `esp32_fan_agent/secrets.h` (copy from
`secrets.h.example`). The sketch compiles for esp32 core 2.0.x (checked) and 3.x.

## Fan controller hardware

- **Switching device:** SSR-25DA solid-state relay (3–32 V DC input, so a 3.3 V GPIO drives it directly; silent).
  Input + to the controller pin, input − to its GND; output in series with the fan's live wire (electrician).
- **Raspberry Pi:** `pi_fan_agent/` (`relay_check.py` = simple relay test, `pi_fan_agent.py` = the agent, systemd unit).
- **ESP32 (alternative):** `esp32_fan_agent/esp32_fan_agent.ino`, same network API; GPIO 26 + 10 kΩ pull-down to GND.
- The app talks to whichever is set in `config.yaml` → `fan: remote: url:`. Connect only one controller to the SSR.

## Calibration

1. **Noise thresholds.** Set `analytics: debug_log: true`, record a few minutes with
   people sitting still and a few with people walking, then `python run.py --calibrate`
   (or the diagnostics view). Set `activity_noise` just above the activity of the still
   persons and `speed_noise` just above their speed (box jitter). Keep `debug_log` off
   afterwards (the file grows quickly).
2. **Motion index.** `--calibrate` prints the 90th percentile of the lab's total motion;
   use it as `index_ref` so a busy minute shows about 63.
3. **Detection areas.** Draw them around the floor of each work zone; check on the Cameras
   page that persons outside are grey and "no (ignored)".
4. **Pollutant thresholds.** Compare a few weeks of readings (Air quality page, CSV) with
   the lab's exposure limits and with the moments the fan was actually needed; set `start`
   where action is needed and `stop` clearly lower. Change one value at a time and watch
   the fan events.

## Database (`iaq_cctv.db`)

| Table / view | Content |
|---|---|
| `camera_minute` | per camera per minute: occupancy, moving, total motion, temporal variance, index, per-person mean/variance, speed, IDs, pictures, original H<sub>t</sub> |
| `camera_second` | per analysed picture: persons in area, detected, moving, total motion, original score (7 days) |
| `person_obs` | per tracked person per picture: ID, confidence, in area, flow, activity raw/filtered, speed raw/filtered, moving, box (7 days; no pictures stored) |
| `zone_person_minute` | per zone and `_inside` per minute, pooled over person observations |
| `zone_minute`, `inside_minute` | views per zone and for the whole lab |
| `door_events`, `door_minute` | door crossings, per minute in / out / inside |
| `sensor_readings`, `sensor_fields`, `sensor_minute` | ThingSpeak |
| `forecasts`, `forecast_models` | forecasts and model checks |
| `events` | event log: system, camera, sensor, threshold, forecast, roi, occupancy, fan_mode, fan_command, fan_decision, fan_error, controller, error |

Log files: `logs/fan.log`, `logs/analytics_debug.log` (when enabled). Older databases are
upgraded automatically at start; old motion values (pixels) stay in their columns and the
new motion columns start empty.

## Not verified here

Tested in the cloud sandbox with recorded videos, a mock ThingSpeak and the Pi agent in
`--mock` mode on the same computer: tracking, areas, statistics, all pages, Manual/AI,
token, watchdog. Not tested: the real relay on a Raspberry Pi, the real fan, access from a
second device on the lab network, and the forecast's accuracy on the lab's data.
