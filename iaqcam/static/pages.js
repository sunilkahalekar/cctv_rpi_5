/* The seven pages. Few words, values first; details sit in tooltips (hover / long-press).
   Every value comes from the app; missing data shows "–" or "Unavailable", never 0. */
(function () {
  "use strict";
  const { $, $$, el, fmt, sci, ms, dayClock, table, chip, status, mark, cameraLabel, icon } = IAQ;
  const S = IAQ.state;
  const LEVEL = { info: "info", warning: "warn", critical: "bad" };
  const NA = "–";

  let over = null;          // /api/overview (pollutant tiles)
  async function loadOverview() {
    try { over = await IAQ.getJSON("/api/overview?" + IAQ.rangeQuery()); } catch (e) { /* keep the last */ }
    return over;
  }
  function defaultField() {
    if (!over || !over.tiles.length) return null;
    if (!S.field || !over.tiles.some((t) => t.key === S.field)) S.field = (over.tiles.find((t) => t.kind === "pm10") || over.tiles[0]).key;
    return S.field;
  }
  function scopeOptions(select) {
    const live = S.live;
    if (!live) return;
    const opts = [["inside", "Whole lab"]];
    if ((live.door || {}).enabled) opts.push(["door", "Lab (door count)"]);
    live.zones.forEach((z) => opts.push(["zone:" + z.key, z.name]));
    live.cameras.forEach((c) => opts.push(["camera:" + c.id, "C" + c.id + " " + (c.location || c.name)]));
    const sig = opts.map((o) => o.join("=")).join("|");
    if (select.dataset.sig !== sig) { select.dataset.sig = sig; select.replaceChildren(...opts.map(([v, t]) => new Option(t, v))); }
  }
  function cameraOptions(select, value) {
    const live = S.live;
    if (!live) return;
    const sig = live.cameras.map((c) => c.id).join(",");
    if (select.dataset.sig !== sig) { select.dataset.sig = sig; select.replaceChildren(...live.cameras.map((c) => new Option("C" + c.id + " " + (c.location || c.name), c.id))); }
    if (value) select.value = value;
  }
  function dot(kind, title) { const d = el("span", "dot " + kind); if (title) d.title = title; return d; }
  function freshDot(t) {
    if (!t || t.value === null) return dot("off", "no reading");
    if (t.fresh === false) return dot("warn", "stale: last reading " + (t.reading_ts || "") + " (" + Math.round(t.age / 60) + " min ago)");
    return dot("ok", "fresh: " + (t.reading_ts || ""));
  }
  // stat card: icon, number, a few words; the explanation is the tooltip
  function card(ic, label, value, sub, title, cls, extra) {
    const c = el("div", "card stat" + (cls ? " " + cls : ""));
    const top = el("div", "stattop");
    top.append(icon(ic), el("span", "label", label));
    c.append(top);
    const row = el("div", "statrow");
    const v = el("div", "value");
    if (value instanceof Node) v.append(value); else v.textContent = value;
    row.append(v);
    if (extra) row.append(extra);
    c.append(row);
    if (sub) c.append(el("div", "label", sub));
    if (title) c.title = title;
    return c;
  }
  const onOff = (v) => (v === true ? "ON" : v === false ? "OFF" : "?");
  function evItem(e) {
    const li = el("li", "ev " + (LEVEL[e.level] || ""));
    li.title = e.ts + " · " + e.kind;
    li.append(el("span", "t", e.ts.slice(11, 16)), el("span", "m", e.message));
    return li;
  }

  // ---------------------------------------------------------- pollutant tiles
  function renderTiles(host, onPick) {
    if (!over) return;
    if (!over.tiles.length) { host.replaceChildren(el("div", "card empty", "No pollutant sensor")); return; }
    defaultField();
    const POLL = ["pm1", "pm2_5", "pm10", "co2", "voc"];
    // pollutants always (shown as unavailable when silent); other sensors only when they report
    host.replaceChildren(...over.tiles.filter((t) => POLL.includes(t.kind) || t.value !== null).map((t) => {
      const b = el("button", "card tile" + (t.alert === "above" ? " alarm" : ""));
      b.type = "button";
      b.setAttribute("aria-pressed", t.key === S.field ? "true" : "false");
      const name = el("div", "name");
      name.append(el("b", "", t.label), freshDot(t));
      const v = el("div", "value" + (t.fresh === false ? " stale" : ""));
      v.append(document.createTextNode(t.value === null ? NA : fmt(t.value, t.digits)), el("small", "", " " + t.unit));
      const r = el("div", "row");
      if (t.value === null) r.append(status(null));
      else if (t.band) r.append(status(t.band));
      if (t.delta10 !== null && t.delta10 !== undefined) {
        const eps = 0.5 * Math.pow(10, -t.digits), up = t.delta10 > eps, down = t.delta10 < -eps;
        const tr = el("span", up ? "up" : down ? "down" : "flat", (up ? "▲ " : down ? "▼ " : "● ") + IAQ.signed(up || down ? t.delta10 : 0, t.digits));
        tr.title = "change against 10 minutes ago";
        r.append(tr);
      }
      const valid = t.used === "full" || t.used === "sensors";
      if (t.forecast !== null && t.forecast !== undefined && valid) r.append(el("span", "fc", "→ " + fmt(t.forecast, t.digits)));
      b.append(name, v, r, IAQ.spark(t.spark, valid ? t.spark_forecast : [], over.lead));
      b.title = t.label + (t.reading_ts ? " · " + t.reading_ts : "") + (valid && t.forecast !== null ? " · forecast +" + over.lead + " min " + fmt(t.forecast, t.digits) : " · no valid forecast")
        + (t.fan_limits ? " · fan start " + t.fan_limits.start + " / stop " + t.fan_limits.stop : "") + "\nline = last hour · dashed = forecast";
      b.addEventListener("click", () => onPick(t.key));
      return b;
    }));
  }

  // caption of a camera: not analysed before its setup
  function idleText(c) { return c.setup === "setup" ? "set up first" : c.setup === "skip" ? "off" : null; }

  // ============================================================ 1 OVERVIEW
  const ov = { chart: IAQ.PollutantChart($('[data-chart="ov"]')), cams: {}, fan: IAQ.FanGraphic($("#ovFanPic"), "small") };
  $('[data-chart="ov"] .js-scope').addEventListener("change", (e) => { S.scope = e.target.value; ov.chart.load(S.field, S.scope); });
  function pickOv(k) { S.field = k; renderTiles($("#tiles"), pickOv); ov.chart.load(k, S.scope); }
  async function ovRefresh() { await loadOverview(); renderTiles($("#tiles"), pickOv); ov.chart.load(defaultField(), S.scope); }
  IAQ.page("overview", {
    enter() { ovRefresh(); if (IAQ.loadPhysKeys) IAQ.loadPhysKeys(); if (IAQ.status_) this.status(IAQ.status_); },
    range() { ovRefresh(); },
    slow() { ovRefresh(); if (IAQ.loadPhysKeys) IAQ.loadPhysKeys(); },
    resize() { ov.chart.render(); },
    status(st) {
      const a = $("#ovAlerts");
      const shown = st.alerts.filter((x) => x.level !== "info");
      a.replaceChildren(...(shown.length ? shown.map((x) => { const link = { setup: "#areas", transit: "#warning" }[x.kind]; const d = el(link ? "a" : "div", "alertchip " + LEVEL[x.level]); if (link) { d.href = link; d.title = "open"; } d.append(icon("alert"), el("span", "", x.text)); return d; })
        : [(() => { const d = el("div", "alertchip ok"); d.append(icon("check"), el("span", "", "All normal")); return d; })()]));
      st.alerts.filter((x) => x.level === "info").forEach((x) => a.append(el("span", "mark info", x.text.replace("Fan control is in DRY RUN: no relay is switched", "Fan: dry run"))));
      const L = st.live, m = st.minute, f = st.fan, sen = st.sensors;
      const camsOk = st.cameras.online === st.cameras.total;
      const senOk = sen.age !== null && sen.age <= sen.stale_after;
      $("#ovCards").replaceChildren(
        card("person", "People", L.persons === null ? NA : String(L.persons), m ? "avg " + fmt(m.persons_mean, 1) + " /min" : "", "persons inside the detection areas (inside cameras, newest pictures)"),
        card("run", "Moving", L.moving === null ? NA : String(L.moving), L.persons ? Math.round(100 * L.moving / L.persons) + " % of people" : "", "persons moving above the noise threshold"),
        card("gauge", "Motion", "", L.motion === null ? "" : fmt(L.motion, 2) + " BH/s", "motion index 0-100 (total body movement); " + st.index_ref + " BH/s = 63", "", IAQ.ring(L.index, 100, "motion index")),
        card("wave", "Variation", L.variance_60s === null ? NA : sci(L.variance_60s), "last 60 s", "temporal variance of the total motion, (BH/s)²"),
        card("camera", "Cameras", st.cameras.online + "/" + st.cameras.total, camsOk ? "live" : "offline: " + (st.cameras.total - st.cameras.online), "", camsOk ? "" : "warn"),
        card("sensor", "Sensors", sen.last ? sen.last.slice(11, 16) : NA, senOk ? "fresh" : "stale", sen.error || (sen.channel || ""), senOk ? "" : "warn"),
        card("cpu", "System", st.health.round_seconds === null || st.health.round_seconds === undefined ? "…" : fmt(st.health.round_seconds, 1) + " s", "per round", st.health.forecast || ""));
      if (f) {
        ov.fan.set(f);
        $("#ovFanChips").replaceChildren(chip("Relay", f.signal === true ? "ON" : f.signal === false ? "OFF" : NA, "level read back from GPIO " + ((f.gpio || {}).pin ?? 9)),
          chip("Link", f.ok === false ? "error" : "ok", f.ok === false ? f.error : f.controller, f.ok === false ? "bad" : ""));
        $("#ovFan").title = (f.decision || {}).reason || "";
      } else { $("#ovFanChips").replaceChildren(el("span", "label", "Fan control off")); }
      $("#ovEvents").replaceChildren(...(st.events.length ? st.events.slice(0, 7).map(evItem) : [el("li", "empty", "No events")]));
    },
    live(live) {
      scopeOptions($('[data-chart="ov"] .js-scope'));
      $('[data-chart="ov"] .js-scope').value = S.scope;
      $("#ovZones").replaceChildren(...live.zones.map((z) => {
        const d = el("div", "zone" + (z.inside ? "" : " outside"));
        d.title = z.inside ? "inside the lab" : "outside the lab (not counted)";
        const n = el("b", "", z.count === null ? NA : String(z.count));
        d.append(el("span", "zn", z.name), n);
        if (z.moving) d.append(el("span", "mv", z.moving + " moving"));
        return d;
      }));
      const host = $("#ovCams");
      live.cameras.forEach((c) => {
        let v = ov.cams[c.id];
        if (!v) {
          const cell = el("button", "camcell"); cell.type = "button";
          const view = el("div"), cap = el("div", "cap");
          cell.append(view, cap); host.append(cell);
          cell.addEventListener("click", () => { IAQ.selectCamera(c.id); IAQ.go("#cameras"); });
          v = ov.cams[c.id] = { view: IAQ.CamView(view, { small: true }), cap: cap };
        }
        v.cap.replaceChildren(el("span", "", "C" + c.id), el("b", "", !c.online ? "offline" : idleText(c) || (c.analysed ? c.count + " · " + c.moving + " moving" : "…")));
        v.cap.title = cameraLabel(c);
        v.view.set(c);
      });
    },
  });

  // ============================================================ 2 CAMERAS
  const cm = { big: IAQ.CamView($("#cmBig")), grid: {}, hi: null, histCam: null };
  $$("#cmToggles input").forEach((i) => {
    i.checked = IAQ.viewOpts[i.dataset.opt] !== false;
    i.addEventListener("change", () => { IAQ.viewOpts[i.dataset.opt] = i.checked; IAQ.saveViewOpts(); cm.big.redraw(); Object.values(cm.grid).forEach((g) => g.view.redraw()); });
  });
  $("#cmBig").addEventListener("click", () => openZoom(S.cam));
  function selectedCam(live) {
    if (!live.cameras.some((c) => c.id === S.cam)) {
      const busy = live.cameras.filter((c) => c.online).sort((a, b) => (b.count || 0) - (a.count || 0))[0];
      S.cam = (busy || live.cameras[0] || {}).id;
    }
    return live.cameras.find((c) => c.id === S.cam);
  }
  async function cmHistory() {
    if (!S.cam) return;
    $("#cmHistTitle").textContent = "C" + S.cam + " history";
    try { IAQ.multiChart($("#cmHist"), await IAQ.getJSON("/api/chart?" + IAQ.rangeQuery() + "&scope=camera:" + S.cam), IAQ.MOTION_PANELS.slice(0, 5)); } catch (e) { /* keep */ }
  }
  function mini(ic, label, value, title) {
    const d = el("div", "mini");
    d.title = title || "";
    const t = el("span", "label"); t.append(icon(ic), document.createTextNode(label));
    d.append(t, el("b", "", value));
    return d;
  }
  IAQ.page("cameras", {
    enter(params) { if (params.get("cam")) IAQ.selectCamera(+params.get("cam")); cm.histCam = null; loadDoor(); },
    range() { cmHistory(); },
    slow() { cmHistory(); },
    resize() { cmHistory(); drawDoorLine(); },
    async live(live) {
      const c = selectedCam(live);
      if (c && cm.histCam !== c.id) { cm.histCam = c.id; cmHistory(); }
      if (c) {
        $("#cmTitle").textContent = cameraLabel(c);
        $("#cmSub").replaceChildren(
          mark(c.online ? "ok" : "bad", c.online ? "LIVE" : "OFFLINE"),
          mark("info", c.analysed ? "⟳ " + c.analysed : "waiting", "last processed picture"),
          ...(c.frame_size ? [mark("info", c.frame_size[0] + "×" + c.frame_size[1])] : []),
          mark({ area: "ok", full: "info", skip: "off", setup: "bad" }[c.setup] || "off", { area: "detection area", full: "whole picture", skip: "not analysed", setup: "SET UP FIRST" }[c.setup] || "–",
            { area: "persons count only inside the detection areas", full: "the whole picture is the detection area", skip: "set up as not analysed", setup: "not analysed until its detection area is saved (Setup)" }[c.setup] || ""));
        $("#cmEditRoi").href = "#areas?cam=" + c.id;
        cm.big.set(c);
        const m = c.minute, roll = c.rolling || {}, ok = c.online && c.analysed && !idleText(c);
        $("#cmStats").replaceChildren(
          mini("person", "In area", ok ? String(c.count) : NA, "persons whose feet are inside an area (detected in picture: " + (ok ? c.detected : NA) + ")"),
          mini("run", "Moving", ok ? String(c.moving) : NA),
          mini("gauge", "Motion", c.index === null || c.index === undefined ? NA : fmt(c.index, 0), "motion index 0-100 · total " + fmt(c.total, 3) + " BH/s"),
          mini("wave", "σ² 60 s", roll.var === null || roll.var === undefined ? NA : sci(roll.var), (roll.n || 0) + " pictures; needs ≥ 2"),
          mini("clock", "Last min", m ? fmt(m.motion_index, 0, NA) : NA, m ? m.window_start + " · σ² " + sci(m.motion_total_var, "n/a") + " · " + m.n_samples + " pictures" : ""),
          mini("camera", "Held", ok ? String(c.held || 0) : NA, "persons sitting in the area that YOLO missed in this picture: still counted, same ID"));
        const ppl = (c.online && c.people) || [];
        table($("#cmPeople"), ["ID", "Conf", "Area", "State", "Act.", "Speed"],
          ppl.map((p) => ["#" + p.id, p.held ? "held" : p.new ? "new" : fmt(p.conf, 2), p.in_roi ? "✓" : "✕", p.held ? "⏸ " + Math.round(p.since) + " s" : p.moving ? "moving" : "still", fmt(p.activity, 2, NA), p.speed === null ? NA : fmt(p.speed, 2)]));
        const roi = await IAQ.roi(c.id);
        $("#cmAreas").replaceChildren(...roi.polygons.map((p, i) => {
          const b = el("button", "chip" + (cm.hi === i ? " sel" : "") + (p.enabled ? "" : " undet"), p.name);
          b.type = "button";
          b.addEventListener("click", () => { cm.hi = cm.hi === i ? null : i; cm.big.highlight(cm.hi); });
          return b;
        }));
      }
      const host = $("#cmGrid");
      live.cameras.forEach((cam) => {
        let g = cm.grid[cam.id];
        if (!g) {
          const cell = el("button", "camcell"); cell.type = "button";
          const view = el("div"), cap = el("div", "cap");
          cell.append(view, cap); host.append(cell);
          cell.addEventListener("click", () => { IAQ.selectCamera(cam.id); cm.hi = null; this.live(S.live); window.scrollTo({ top: 0, behavior: "smooth" }); });
          g = cm.grid[cam.id] = { view: IAQ.CamView(view, { small: true }), cap: cap, cell: cell };
        }
        g.cell.classList.toggle("sel", cam.id === S.cam);
        g.cap.replaceChildren(el("span", "", "C" + cam.id + " " + (cam.location || "")),
          el("b", "", !cam.online ? "offline" : idleText(cam) || (cam.analysed ? cam.count + " · " + cam.moving + " moving · " + fmt(cam.index, 0, NA) : "…")));
        g.cap.title = cam.analysed ? "processed " + cam.analysed + " · motion index " + fmt(cam.index, 0, NA) + " · σ² " + sci((cam.rolling || {}).var) : "";
        g.view.set(cam);
      });
      renderDoor(live);
      table($("#camMinutes"), ["Cam", "Min", "Pics", "Count", "Max", "Moving", "BH/s", "Index", "σ² time", "σ² persons", "IDs"],
        live.cameras.map((c) => { const m = c.minute; return ["C" + c.id, m ? m.window_start.slice(11, 16) : NA, m ? String(m.n_samples) : NA, m ? fmt(m.n_person_mode, 0, NA) : NA, m ? fmt(m.n_person, 0) : NA, m ? fmt(m.n_moving_mean, 1) : NA, m ? fmt(m.motion_total, 3, NA) : NA, m ? fmt(m.motion_index, 0, NA) : NA, m ? sci(m.motion_total_var, NA) : NA, m ? sci(m.sigma2_person, NA) : NA, m ? fmt(m.unique_persons, 0) : NA]; }));
      if (zoom.id !== null) zoomUpdate();
    },
  });

  // enlarged picture
  const zoom = { id: null, view: IAQ.CamView($("#zoomView")) };
  function openZoom(id) { zoom.id = id; $("#zoom").hidden = false; zoomUpdate(true); $("#zoomClose").focus(); }
  function zoomUpdate(force) {
    const c = S.live && S.live.cameras.find((x) => x.id === zoom.id);
    if (!c) return;
    $("#zoomText").textContent = cameraLabel(c) + " · " + (!c.online ? "offline" : idleText(c) || (c.analysed ? c.count + " in area · " + c.moving + " moving · " + c.analysed : "…"));
    zoom.view.set(c, force);
  }
  function closeZoom() { zoom.id = null; $("#zoom").hidden = true; }
  $("#zoom").addEventListener("click", closeZoom);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && zoom.id !== null) closeZoom(); });
  IAQ.onLive = () => { if (zoom.id !== null) zoomUpdate(); };

  // lab door
  const door = { line: null, inside: null, editing: false, draft: [], enabled: false };
  function renderDoor(live) {
    const d = live.door || {};
    $("#doorCard").hidden = !d.enabled;
    if (!d.enabled) return;
    const last = d.last_event;
    $("#doorSub").textContent = "C" + d.camera + (d.active ? " · tracking" : "") + (last ? " · last " + last.direction + " " + last.ts.slice(11, 16) : "");
    $("#doorSub").title = "counts since " + (d.since || "");
    $("#doorIn").textContent = fmt(d.n_in, 0); $("#doorOut").textContent = fmt(d.n_out, 0); $("#doorInside").textContent = fmt(d.inside, 0);
    if (IAQ.showVideo && !door.editing) {
      const probe = new Image();
      probe.onload = () => { $("#doorImg").src = probe.src; };
      probe.src = "/frame/door.jpg?t=" + Date.now();
    }
  }
  function drawDoorLine() {
    const sv = $("#doorSvg"), frame = $("#doorFrame"), line = door.editing ? door.draft : door.line;
    sv.replaceChildren();
    frame.querySelectorAll(".doortag").forEach((t) => t.remove());
    if (!line || !line.length) return;
    const P = line.map(([x, y]) => [x * 1000, y * 1000]);
    const stroke = (a, b, colour, width) => IAQ.svg("line", { x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: colour, "stroke-width": width, "stroke-linecap": "round", "vector-effect": "non-scaling-stroke" });
    P.forEach(([x, y]) => sv.append(IAQ.svg("circle", { cx: x, cy: y, r: 9, fill: "#ffd400", stroke: "#000", "stroke-width": 2, "vector-effect": "non-scaling-stroke" })));
    if (P.length < 2) return;
    sv.append(stroke(P[0], P[1], "#000", 7), stroke(P[0], P[1], "#ffd400", 4));
    if (!door.inside) return;
    const W = frame.clientWidth || 16, H = frame.clientHeight || 9;
    const a = [line[0][0] * W, line[0][1] * H], b = [line[1][0] * W, line[1][1] * H], m = [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
    let n = [-(b[1] - a[1]), b[0] - a[0]];
    const len = Math.hypot(n[0], n[1]) || 1;
    n = [n[0] / len, n[1] / len];
    if ((door.inside[0] * W - m[0]) * n[0] + (door.inside[1] * H - m[1]) * n[1] < 0) n = [-n[0], -n[1]];
    const Lp = 0.18 * Math.min(W, H), M = [m[0] / W * 1000, m[1] / H * 1000], E = [(m[0] + n[0] * Lp) / W * 1000, (m[1] + n[1] * Lp) / H * 1000];
    sv.append(stroke(M, E, "#000", 7), stroke(M, E, "#3ddc84", 4));
    const tag = el("div", "doortag", "IN");
    tag.style.left = E[0] / 10 + "%"; tag.style.top = E[1] / 10 + "%";
    frame.append(tag);
  }
  function insideFor(line, reference) {
    const [a, b] = line, mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
    let nx = -(b[1] - a[1]), ny = b[0] - a[0];
    const len = Math.hypot(nx, ny) || 1; nx /= len; ny /= len;
    const ref = reference || [mx, my - 1];
    if ((ref[0] - mx) * nx + (ref[1] - my) * ny < 0) { nx = -nx; ny = -ny; }
    return [Math.min(1, Math.max(0, mx + nx * 0.15)), Math.min(1, Math.max(0, my + ny * 0.15))];
  }
  async function loadDoor() {
    try { const d = await IAQ.getJSON("/api/door"); door.enabled = d.enabled; if (d.enabled) { door.line = d.line; door.inside = d.inside_point; drawDoorLine(); } } catch (e) { /* disabled */ }
  }
  function setDoorEditing(on) {
    door.editing = on; door.draft = [];
    $("#doorFrame").classList.toggle("editing", on);
    $("#doorView").hidden = on; $("#doorEditing").hidden = !on;
    $("#doorHint").textContent = on ? "Click both ends of the line" : "Arrow = into the lab";
    drawDoorLine();
  }
  $("#doorEdit").addEventListener("click", () => setDoorEditing(true));
  $("#doorCancel").addEventListener("click", () => setDoorEditing(false));
  $("#doorFrame").addEventListener("click", (e) => {
    if (!door.editing) return;
    const box = $("#doorFrame").getBoundingClientRect();
    if (door.draft.length >= 2) door.draft = [];
    door.draft.push([Math.round((e.clientX - box.left) / box.width * 1000) / 1000, Math.round((e.clientY - box.top) / box.height * 1000) / 1000]);
    $("#doorHint").textContent = door.draft.length === 1 ? "Now the other end" : "Arrow must point into the lab, then save";
    if (door.draft.length === 2) door.inside = insideFor(door.draft, door.inside);
    drawDoorLine();
  });
  $("#doorSave").addEventListener("click", async () => {
    if (door.draft.length !== 2) { $("#doorHint").textContent = "Click two points first"; return; }
    const res = await IAQ.send("POST", "/api/door/line", { line: door.draft, inside_point: door.inside });
    if (!res.ok) { $("#doorHint").textContent = "Not saved: " + (res.error || "invalid line"); return; }
    door.line = door.draft; setDoorEditing(false);
  });
  $("#doorSwap").addEventListener("click", async () => {
    if (!door.line) return;
    const [a, b] = door.line, mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2;
    const flipped = [Math.min(1, Math.max(0, 2 * mx - door.inside[0])), Math.min(1, Math.max(0, 2 * my - door.inside[1]))];
    const res = await IAQ.send("POST", "/api/door/line", { line: door.line, inside_point: flipped });
    if (res.ok) { door.inside = flipped; drawDoorLine(); }
  });
  $("#doorSetForm").addEventListener("submit", async (e) => {
    e.preventDefault();
    const value = $("#doorSetValue").value;
    if (value === "") return;
    const res = await IAQ.send("POST", "/api/door/inside", { value: Number(value) });
    $("#doorSetMsg").textContent = res.ok ? "✓ " + value : (res.error || "Not set");
    if (res.ok) $("#doorSetValue").value = "";
  });
  $("#doorImg").addEventListener("load", drawDoorLine);

  // ============================================================ 3 SETUP
  // Every camera is set up first (detection area, whole picture or off); a camera waiting
  // for its setup is not analysed. The setup can be changed at any time.
  const MODE_NAME = { setup: "Set up first", area: "Area", full: "Whole picture", skip: "Off" };
  const ar = { cam: null, dirty: false, operator: false, mode: "setup", info: null, cells: {}, editor: IAQ.RoiEditor($("#arView"), (what, n) => {
    if (what === "drawing") $("#arHint").textContent = n + " point" + (n > 1 ? "s" : "") + (n >= 3 ? " · Finish when done" : " · keep clicking");
    if (what === "changed") arDirty();
    if (what === "select") arList();
  }) };
  function arMsg(t, cls) { $("#arMsg").textContent = t; $("#arMsg").className = "label " + (cls || ""); }
  function arDirty() { ar.dirty = true; arMsg("unsaved"); arCrop(); }
  function stchip(mode, n) { return el("span", "stchip " + mode, mode === "area" && n ? n + " area" + (n > 1 ? "s" : "") : MODE_NAME[mode] || mode); }
  // preview of the part of the picture YOLO will look at (same rule as roi.py)
  function arCrop() {
    const info = ar.info || {}, polys = ar.editor.polygons.filter((p) => p.enabled);
    if (ar.mode !== "area" || !polys.length || info.detect_in !== "area") { ar.editor.view.setCrop(null); return; }
    const m = info.crop_margin ?? 0.04, h = info.head_room ?? 0.35;
    const rect = (ps) => {
      const xs = ps.flatMap((p) => p.points.map((q) => q[0])), ys = ps.flatMap((p) => p.points.map((q) => q[1]));
      return [Math.max(0, Math.min(...xs) - m), Math.max(0, Math.min(...ys) - h), Math.min(1, Math.max(...xs) + m), Math.min(1, Math.max(...ys) + m)];
    };
    const rects = info.crop_per_area ? polys.map((p) => rect([p])) : [rect(polys)];
    ar.editor.view.setCrop(rects.length === 1 && (rects[0][2] - rects[0][0]) * (rects[0][3] - rects[0][1]) > 0.9 ? null : rects);
  }
  function arModeShow() {
    $$("#arMode button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === ar.mode)));
    $("#arAreaTools").hidden = ar.mode !== "area";
    const lay = $("#arView").closest(".arlayout");
    lay.classList.toggle("mode-full", ar.mode === "full"); lay.classList.toggle("mode-skip", ar.mode === "skip");
    ar.editor.setReadOnly(!ar.operator || ar.mode !== "area");
    $("#arHint").textContent = { setup: "Choose: Area, Whole picture or Off", area: ar.editor.polygons.length ? "People count when their feet are inside an area" : "+ New area, then click the corners",
      full: "Every person in the picture counts", skip: "This camera is not analysed" }[ar.mode];
    arCrop();
  }
  function arList() {
    const ed = ar.editor, list = $("#arList");
    list.replaceChildren(...ed.polygons.map((p, i) => {
      const li = el("li", i === ed.selected ? "sel" : "");
      const name = el("input"); name.value = p.name; name.maxLength = 40; name.disabled = !ar.operator; name.setAttribute("aria-label", "area name");
      name.addEventListener("input", () => { p.name = name.value; arDirty(); ed.redraw(); });
      const on = el("label", "switch"), cb = el("input"); cb.type = "checkbox"; cb.checked = p.enabled; cb.disabled = !ar.operator;
      cb.title = "enabled";
      cb.addEventListener("change", () => { p.enabled = cb.checked; arDirty(); ed.redraw(); });
      on.append(cb, el("span"));
      const edit = el("button", "ghost", "✎"); edit.type = "button"; edit.title = "move corners";
      edit.addEventListener("click", () => { ed.select(i); arList(); $("#arHint").textContent = "Drag the white corners"; });
      const del = el("button", "ghost", "🗑"); del.type = "button"; del.title = "delete"; del.disabled = !ar.operator;
      del.addEventListener("click", () => { ed.remove(i); arDirty(); arList(); });
      li.append(name, on, edit, del);
      return li;
    }));
    if (!ed.polygons.length) list.append(el("li", "empty", "No area yet"));
  }
  async function suLoad() {
    try { ar.info = await IAQ.getJSON("/api/setup"); } catch (e) { return; }
    const sm = ar.info.summary;
    $("#suCount").textContent = sm.done + " / " + sm.total;
    $("#suBar").style.width = (sm.total ? 100 * sm.done / sm.total : 0) + "%";
    $("#suBar").closest(".setprog").classList.toggle("todo", sm.pending.length > 0);
    $("#suModel").textContent = "Model " + (ar.info.model || "?") + " · " + (ar.info.classes || "") + " · YOLO looks " + (ar.info.detect_in === "area" ? "around the areas only" : "at the whole picture")
      + (ar.info.require_setup ? "" : " · setup not required (config)");
    ar.info.cameras.forEach((c) => { const cell = ar.cells[c.id]; if (cell) cell.chip.replaceWith(cell.chip = stchip(c.mode, c.areas)); });
    if (ar.cam) { const me = ar.info.cameras.find((c) => c.id === ar.cam); if (me) arWho(me); }
    arCrop();
  }
  function arWho(c) {
    $("#arTitle").replaceChildren(document.createTextNode("C" + c.id + " " + (c.location || c.name) + " "), stchip(c.mode, c.areas));
    $("#arWho").textContent = c.updated ? "saved " + c.updated + (c.by ? " by " + c.by : "") : "not set up yet";
  }
  function suGrid(live) {
    const host = $("#suGrid");
    live.cameras.forEach((c) => {
      let g = ar.cells[c.id];
      if (!g) {
        const cell = el("button", "sucell"); cell.type = "button";
        const view = el("div"), cap = el("div", "cap"), chip = stchip(c.setup || "setup");
        cap.append(el("b", "", "C" + c.id + " " + (c.location || "")), chip);
        cell.append(view, cap); host.append(cell);
        cell.addEventListener("click", () => arPick(c.id));
        g = ar.cells[c.id] = { cell: cell, view: IAQ.CamView(view, { small: true }), chip: chip };
      }
      g.cell.classList.toggle("sel", c.id === ar.cam);
      g.view.set(c);
    });
  }
  function arPick(cam) {
    if (cam === ar.cam) return;
    if (ar.dirty && !confirm("Discard unsaved changes of C" + ar.cam + "?")) { $("#arCam").value = ar.cam; return; }
    cameraOptions($("#arCam"), cam);
    arLoad(cam);
    Object.entries(ar.cells).forEach(([id, g]) => g.cell.classList.toggle("sel", +id === cam));
  }
  async function arLoad(cam) {
    ar.cam = cam; ar.dirty = false; arMsg("");
    IAQ.forgetRoi(cam);
    const roi = await IAQ.roi(cam, true);
    ar.operator = !!roi.operator;
    ar.mode = roi.configured ? roi.mode : "setup";
    ar.editor.load(roi.polygons);
    $("#arRO").hidden = ar.operator;
    $("#arRO").textContent = "View only (operators: fan → operators in config.yaml)";
    ["#arNew", "#arSave", "#arReload", "#arReset"].forEach((id) => { $(id).disabled = !ar.operator; });
    $$("#arMode button").forEach((b) => { b.disabled = !ar.operator; });
    $("#arReset").hidden = !roi.configured;
    arWho({ id: cam, location: (S.live && (S.live.cameras.find((x) => x.id === cam) || {}).location) || "", name: "", mode: ar.mode,
      areas: roi.polygons.filter((p) => p.enabled).length, updated: roi.updated, by: roi.by });
    arList(); arModeShow();
    const c = S.live && S.live.cameras.find((x) => x.id === cam);
    if (c) ar.editor.view.set(c, true);
  }
  $("#arCam").addEventListener("change", (e) => arPick(+e.target.value));
  $$("#arMode button").forEach((b) => b.addEventListener("click", () => {
    if (ar.mode === b.dataset.mode) return;
    ar.mode = b.dataset.mode; arDirty(); arModeShow();
  }));
  $("#arNew").addEventListener("click", () => {
    ar.editor.startDrawing();
    $("#arNew").hidden = true; $("#arFinish").hidden = false; $("#arCancel").hidden = false;
    $("#arHint").textContent = "Click the corners on the picture";
  });
  function arStopDrawing() { $("#arNew").hidden = false; $("#arFinish").hidden = true; $("#arCancel").hidden = true; }
  $("#arFinish").addEventListener("click", () => {
    if (!ar.editor.finish("Area " + (ar.editor.polygons.length + 1))) { $("#arHint").textContent = "At least 3 points"; return; }
    arStopDrawing(); arDirty(); arList();
    $("#arHint").textContent = "Name it, then Save";
  });
  $("#arCancel").addEventListener("click", () => { ar.editor.cancel(); arStopDrawing(); $("#arHint").textContent = "Cancelled"; });
  $("#arReload").addEventListener("click", () => arLoad(ar.cam));
  $("#arSave").addEventListener("click", async () => {
    if (ar.mode === "setup") { arMsg("choose Area, Whole picture or Off", "bad"); return; }
    if (ar.editor.drawing) { arMsg("Finish or cancel the area first", "bad"); return; }
    $("#arSave").disabled = true;
    const res = await IAQ.send("PUT", "/api/roi/" + ar.cam, { polygons: ar.editor.polygons, mode: ar.mode });
    $("#arSave").disabled = !ar.operator;
    if (!res.ok) { arMsg("✕ " + (res.error || ""), "bad"); return; }
    IAQ.forgetRoi(ar.cam);
    ar.dirty = false; ar.editor.load(res.polygons); arList();
    arMsg("✓ saved · " + (res.mode === "skip" ? "not analysed" : "detection on"), "ok");
    $("#arReset").hidden = false;
    await suLoad();
    // next camera that still waits for its setup
    const next = ar.info && ar.info.summary.pending[0];
    if (next !== undefined) $("#arHint").textContent = "Next: C" + next + " waits for setup";
  });
  $("#arReset").addEventListener("click", async () => {
    if (!confirm("Remove the setup of C" + ar.cam + "? It stops being analysed until it is set up again.")) return;
    const res = await IAQ.send("DELETE", "/api/roi/" + ar.cam, {});
    if (!res.ok) { arMsg("✕ " + (res.error || ""), "bad"); return; }
    await arLoad(ar.cam); await suLoad();
    arMsg("setup removed", "");
  });
  window.addEventListener("beforeunload", (e) => { if (ar.dirty) { e.preventDefault(); e.returnValue = ""; } });
  IAQ.setupBadge = function (live) {
    const n = live && live.setup ? live.setup.pending.length : 0;
    const b = $("#navSetup"); b.hidden = !n; b.textContent = n;
    b.title = n ? n + " camera(s) wait for setup" : "";
  };
  IAQ.page("areas", {
    async enter(params) {
      await suLoad();
      const pending = ar.info ? ar.info.summary.pending : [];
      const cam = +(params.get("cam") || ar.cam || pending[0] || (S.live && S.live.cameras[0] && S.live.cameras[0].id) || 0);
      if (S.live) { cameraOptions($("#arCam"), cam); suGrid(S.live); }
      if (cam && cam !== ar.cam) arLoad(cam);
    },
    slow() { suLoad(); },
    live(live) {
      suGrid(live);
      if (!ar.cam && live.cameras.length) { const p = (live.setup || {}).pending || []; arPick(p.length ? p[0] : live.cameras[0].id); return; }
      cameraOptions($("#arCam"), ar.cam);
      const c = live.cameras.find((x) => x.id === ar.cam);
      if (c && !ar.editor.drawing) ar.editor.view.set(c);
    },
    resize() { ar.editor.redraw(); },
  });

  // ============================================================ 4 MOTION
  const an = { scope: "inside" };
  $("#anScope").addEventListener("change", (e) => { an.scope = e.target.value; anCharts(); });
  $("#anPosCam").addEventListener("change", () => { anHeat(); anPersons(); });
  $("#anInfo").addEventListener("click", () => { const b = $("#anHow"); b.hidden = !b.hidden; $("#anInfo").setAttribute("aria-expanded", String(!b.hidden)); });
  async function anCharts() {
    let d;
    try { d = await IAQ.getJSON("/api/chart?" + IAQ.rangeQuery() + "&scope=" + encodeURIComponent(an.scope)); } catch (e) { return; }
    IAQ.multiChart($("#anCharts"), d, IAQ.MOTION_PANELS);
    const last = d.rows.filter((r) => r.persons !== undefined).pop();
    const g = (k, digits) => (last && last[k] !== undefined && last[k] !== null ? (digits === "sci" ? sci(last[k]) : fmt(last[k], digits)) : NA);
    $("#anCards").replaceChildren(
      card("person", "People", g("persons_mean", 1), last ? "max " + last.persons : "", "mean persons in the areas, minute " + (last ? last.t.slice(11, 16) : "")),
      card("run", "Moving", g("moving", 1), "", "persons moving, mean per picture"),
      card("gauge", "Motion", "", g("motion", 2) + " BH/s", "total body movement; ring = index 0-100", "", IAQ.ring(last ? last.index : null, 100, "motion index")),
      card("wave", "σ² time", g("sigma2", "sci"), "(BH/s)²", "variation of the total motion within the minute"),
      card("spread", "σ² people", g("sigma2_person", "sci"), "(BH/s)²", "differences between persons"),
      card("run", "Speed", g("speed", 2), "BH/s", "walking speed, mean of tracked persons"));
  }
  async function anHeat() {
    const cam = +$("#anPosCam").value;
    if (!cam) return;
    let d;
    try { d = await IAQ.getJSON("/api/positions?camera=" + cam + "&" + IAQ.rangeQuery()); } catch (e) { return; }
    const host = $("#anHeat");
    if (!d.total) { host.replaceChildren(el("div", "empty", "No positions in this range")); return; }
    const max = Math.max(...d.grid.flat());
    const W = 640, H = Math.round(W * (d.frame_size ? d.frame_size[1] / d.frame_size[0] : 9 / 16));
    const s = IAQ.svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "where people stood" });
    const cw = W / d.nx, ch = H / d.ny;
    const c = S.live && S.live.cameras.find((x) => x.id === cam);
    if (c && c.frame_seq && IAQ.showVideo) s.append(IAQ.svg("image", { href: "/frame/" + cam + ".jpg?seq=" + c.frame_seq, x: 0, y: 0, width: W, height: H, preserveAspectRatio: "none", opacity: 0.45 }));
    else s.append(IAQ.svg("rect", { x: 0, y: 0, width: W, height: H, fill: "var(--grid)" }));
    d.grid.forEach((row, i) => row.forEach((n, j) => {
      if (!n) return;
      const r = IAQ.svg("rect", { x: j * cw, y: i * ch, width: cw - 0.5, height: ch - 0.5, fill: "var(--s2)", opacity: (0.2 + 0.75 * Math.sqrt(n / max)).toFixed(2) });
      r.append(IAQ.svg("title", {}, n + " observations"));
      s.append(r);
    }));
    const roi = await IAQ.roi(cam);
    roi.polygons.filter((p) => p.enabled).forEach((p) => s.append(IAQ.svg("polygon", { points: p.points.map(([x, y]) => x * W + "," + y * H).join(" "), fill: "none", stroke: "#ffd400", "stroke-width": 2.5 })));
    host.replaceChildren(s, el("div", "label", d.total + " observations"));
  }
  function anPersons() {
    const cam = S.live && S.live.cameras.find((c) => c.id === +$("#anPosCam").value);
    const ppl = (cam && cam.online && cam.people) || [];
    table($("#anPersons"), ["ID", "Area", "Act.", "Speed", ""],
      ppl.map((p) => ["#" + p.id, p.in_roi ? "✓" : "✕", fmt(p.activity, 2, NA), p.speed === null ? NA : fmt(p.speed, 2), p.held ? "held" : p.moving ? "moving" : "still"]));
  }
  async function anDiag() {
    const cam = +$("#anDiagCam").value, mins = $("#anDiagMin").value, body = $("#anDiagBody");
    const r = await fetch("/api/diagnostics?camera=" + cam + "&minutes=" + mins, { cache: "no-store" });
    if (r.status === 403) { $("#anDiagNote").textContent = "Operators only"; body.replaceChildren(); return; }
    if (!r.ok) { $("#anDiagNote").textContent = "Unavailable (" + r.status + ")"; return; }
    const d = await r.json(), st = d.settings, live = d.live || {};
    $("#anDiagNote").textContent = "noise: activity " + st.activity_noise + " · speed " + st.speed_noise + " BH/s · flow Δt " + st.flow_interval + " s · window " + st.window_seconds + " s · now: " + (live.count ?? NA) + "/" + (live.detected ?? NA) + " in area, " + (live.moving ?? NA) + " moving";
    const t1 = el("table"), t2 = el("table"), t3 = el("table");
    table(t1, ["Time", "ID", "Conf", "Area", "Flow px", "Box h", "Act. raw", "Act.", "Speed raw", "Speed", "Moving"],
      d.persons.map((p) => [p.ts.slice(11), "#" + p.pid, fmt(p.conf, 2), p.in_roi ? "✓" : "✕", fmt(p.flow_px, 2, NA), String(p.height), fmt(p.activity_raw, 4, NA), fmt(p.activity, 4, NA), fmt(p.speed_raw, 3, NA), fmt(p.speed, 3, NA), p.moving ? "✓" : ""]));
    table(t2, ["Time", "Tracks", "In area", "Moving", "Total BH/s", "Old score px"],
      d.pictures.map((p) => [p.ts.slice(11), String(p.n_detected), String(p.in_area), String(p.n_moving), fmt(p.motion_total, 4, "no flow"), fmt(p.frame_score_px, 3)]));
    table(t3, ["Minute", "Pics", "Mean (recalc)", "Mean (stored)", "σ² (recalc)", "σ² (shown)", ""],
      d.checks.map((c) => [c.window_start.slice(11, 16), String(c.pictures), fmt(c.recomputed_mean, 5, NA), fmt(c.stored_mean, 5, NA), sci(c.recomputed_var, NA), sci(c.stored_var, NA), c.match ? "✓" : "✕"]));
    const h = (t) => el("h3", "sub", t), wrap = (t) => { const w = el("div", "scroll short"); w.append(t); return w; };
    body.replaceChildren(h("Per person"), wrap(t1), h("Per picture"), wrap(t2), h("Stored vs recomputed"), wrap(t3));
  }
  $("#anDiag").addEventListener("toggle", () => { if ($("#anDiag").open) anDiag(); });
  $("#anDiagLoad").addEventListener("click", anDiag);
  IAQ.page("analytics", {
    enter() { anCharts(); setTimeout(anHeat, 300); },
    range() { anCharts(); anHeat(); },
    slow() { anCharts(); anHeat(); },
    resize() { anCharts(); },
    live(live) {
      scopeOptions($("#anScope")); $("#anScope").value = an.scope;
      const first = !$("#anPosCam").options.length;
      cameraOptions($("#anPosCam"), $("#anPosCam").value || S.cam);
      cameraOptions($("#anDiagCam"), $("#anDiagCam").value || S.cam);
      if (first) anHeat();
      anPersons();
    },
  });

  // ============================================================ 5 AIR
  const air = { chart: IAQ.PollutantChart($('[data-chart="air"]')), scope: "inside" };
  $('[data-chart="air"] .js-scope').addEventListener("change", (e) => { air.scope = e.target.value; air.chart.load(S.field, air.scope); });
  $('[data-chart="air"] .js-field').addEventListener("change", (e) => { S.field = e.target.value; airCards(); air.chart.load(S.field, air.scope); });
  function airCards() {
    if (!over) return;
    defaultField();
    const fsel = $('[data-chart="air"] .js-field'), sig = over.tiles.map((t) => t.key).join(",");
    if (fsel.dataset.sig !== sig) { fsel.dataset.sig = sig; fsel.replaceChildren(...over.tiles.map((t) => new Option(t.label, t.key))); }
    fsel.value = S.field || "";
    renderTiles($("#airCards"), (k) => { S.field = k; airCards(); air.chart.load(S.field, air.scope); });
  }
  async function airTimeline() {
    let d;
    try { d = await IAQ.getJSON("/api/events?" + IAQ.rangeQuery() + "&kind=threshold&kind=fan_command&kind=fan_decision&kind=fan_mode&kind=occupancy&kind=sensor&limit=300"); } catch (e) { return; }
    const evs = d.events.filter((e) => e.kind !== "fan_decision" || e.fan_state);
    const host = $("#airTimeline");
    if (!evs.length) { host.replaceChildren(el("div", "empty", "No events in this range")); return; }
    const W = Math.max(320, host.clientWidth || 700), H = 124, L = 74, R = W - 10;
    const nowT = ms(S.live ? S.live.now : evs[0].ts);
    const t0 = S.rec ? ms(S.rec.from) : nowT - S.minutes * 60000, t1 = S.rec ? ms(S.rec.to) : nowT;
    const X = (t) => L + (t - t0) / Math.max(1, t1 - t0) * (R - L);
    const lanes = [["occupancy", "People", "var(--ctx)"], ["threshold", "Pollutant", "var(--s2)"], ["fan", "Fan", "var(--s1)"], ["sensor", "Sensor", "var(--muted)"]];
    const s = IAQ.svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "event timeline" });
    lanes.forEach(([, name], i) => s.append(IAQ.svg("text", { x: L - 8, y: 20 + i * 26, "text-anchor": "end", "font-size": 11, fill: "var(--muted)" }, name),
      IAQ.svg("line", { x1: L, x2: R, y1: 16 + i * 26, y2: 16 + i * 26, stroke: "var(--grid)" })));
    evs.forEach((e) => {
      const lane = e.kind.startsWith("fan") ? 2 : lanes.findIndex((l) => l[0] === e.kind);
      if (lane < 0) return;
      const c = IAQ.svg("circle", { cx: X(ms(e.ts)), cy: 16 + lane * 26, r: 6, fill: e.fan_state === "on" ? "var(--s1)" : e.fan_state === "off" ? "var(--surface)" : lanes[lane][2], stroke: lanes[lane][2], "stroke-width": 2 });
      c.append(IAQ.svg("title", {}, e.ts.slice(11) + " · " + e.message));
      s.append(c);
    });
    s.append(IAQ.svg("text", { x: L, y: H - 4, "font-size": 11, fill: "var(--muted)" }, dayClock(t0)), IAQ.svg("text", { x: R, y: H - 4, "text-anchor": "end", "font-size": 11, fill: "var(--muted)" }, dayClock(t1)));
    host.replaceChildren(s);
  }
  async function airErrors() {
    let data;
    try { data = await IAQ.getJSON("/api/errors?" + IAQ.rangeQuery()); } catch (e) { return; }
    $("#skill").replaceChildren(...data.items.map((i) => {
      const row = el("div", "err"), track = el("div", "track"), bar = el("i");
      bar.style.width = (i.percent ? Math.min(100, i.percent * 2) : 0) + "%";
      track.append(bar);
      row.title = i.n ? "mean error " + fmt(i.error, 2) + " " + i.unit + " over " + i.n + " min" + (i.naive_percent !== null ? " · “no change” " + fmt(i.naive_percent, 1) + " %" : "") : "not checked yet";
      row.append(el("span", "", i.label), track, el("b", "", i.percent !== null ? fmt(i.percent, 1) + " %" : NA));
      return row;
    }));
  }
  async function airAll() { await loadOverview(); airCards(); air.chart.load(defaultField(), air.scope); airTimeline(); airErrors(); }
  IAQ.page("air", {
    enter() { airAll(); },
    range() { airAll(); },
    slow() { airAll(); },
    resize() { air.chart.render(); airTimeline(); },
    live() { scopeOptions($('[data-chart="air"] .js-scope')); $('[data-chart="air"] .js-scope').value = air.scope; },
  });

  // ============================================================ PHYSICS
  // Mass balance of the room air: constants estimated from the data, the five forecasts
  // compared on the same minutes, the inputs (live / future) and the research evaluation.
  const VARIANT = { physics: "Physics", full: "Data", sensors: "Sensor only", greybox: "Grey-box", physreg: "Physics-informed", transit: "In transit",
    "no change": "No change", paused: "Paused" };
  const VARIANT_HINT = { physics: "mass balance only", full: "regression on readings, persons and motion",
    sensors: "regression on the sensor's own readings", greybox: "mass balance + learned rest",
    physreg: "regression trained to obey the mass balance", transit: "camera zones with learned travel times to the sensor (Early warning page)", "no change": "nothing beats the current reading yet",
    paused: "recent forecasts were worse than no change" };
  const ph = { data: null, field: null };
  const per = (v, d) => (v === null || v === undefined ? NA : fmt(v, d === undefined ? 2 : d));

  // key points for the overview card
  function physKeys(d) {
    const pols = (d.pollutants || []).filter((p) => p.model && p.model.removal_per_hour !== null && p.model.removal_per_hour !== undefined);
    if (!pols.length) return [el("span", "label", "Waiting for the first model (needs " + "sensor history)")];
    const co2 = pols.find((p) => p.kind === "co2"), ref = co2 || pols[0], m = ref.model;
    const used = {};
    pols.forEach((p) => { used[p.model.used] = (used[p.model.used] || 0) + 1; });
    const top = Object.entries(used).sort((a, b) => b[1] - a[1])[0][0];
    const best = Math.max(...pols.map((p) => Math.max(...Object.values(p.skill || {}).filter((v) => v !== null), -9)));
    const out = [
      keyFact("Air change", per(m.removal_per_hour, 1) + "/h", m.removal_fan_per_hour !== null && m.removal_fan_per_hour !== undefined ? "fan on " + per(m.removal_fan_per_hour, 1) + "/h" : (m.removal_source === "decay" ? m.n_decays + " decays" : "assumed"), "removal rate of " + ref.label + " with the fan off"),
      keyFact("Forecast", VARIANT[top] || top, best > -9 ? "skill " + fmt(best, 2) : "", "variant used by most pollutants; skill = 1 − error / error of no change"),
    ];
    if (co2 && m.lambda_online_per_hour !== null && m.lambda_online_per_hour !== undefined) out.push(keyFact("Live λ", per(m.lambda_online_per_hour, 1) + "/h", "occupied", "air change estimated from the camera head count and CO₂"));
    if (co2 && m.co2_gen_lpm !== null && m.co2_gen_lpm !== undefined) out.push(keyFact("CO₂/person", per(m.co2_gen_lpm, 2), "L/min", "from the camera count; seated adults ≈ 0.3"));
    const last = (d.events || [])[0];
    if (last) {
      const e = el("div", "kfev " + (LEVEL[last.level] || ""));
      e.title = last.ts + " · " + last.message;
      e.append(el("span", "t", last.ts.slice(11, 16)), el("span", "", last.message));
      out.push(e);
    }
    return out;
  }
  function keyFact(label, value, sub, title) {
    const d = el("div", "kf");
    d.title = title || "";
    d.append(el("span", "label", label), el("b", "", value), el("small", "", sub || ""));
    return d;
  }
  IAQ.loadPhysKeys = async function () {
    try { const d = await IAQ.getJSON("/api/physics?brief=1"); $("#ovPhysKeys").replaceChildren(...physKeys(d)); }
    catch (e) { /* keep */ }
  };

  function skillBars(p) {
    const box = el("div", "skills");
    const m = p.model || {}, sk = p.skill || {};
    const order = [["physics", "physics"], ["data", m.mae_full !== null && m.mae_full !== undefined ? "full" : "sensors"], ["greybox", "greybox"], ["physreg", "physreg"], ["transit", "transit"]];
    order.forEach(([k, name]) => {
      const v = k === "data" ? (sk.data !== null && sk.data !== undefined ? sk.data : sk.sensors) : sk[k];
      const row = el("div", "sk" + (m.used === name || (k === "data" && (m.used === "full" || m.used === "sensors")) ? " used" : ""));
      row.title = VARIANT_HINT[name] + (v === null || v === undefined ? "" : " · skill " + fmt(v, 2) + " (1 = perfect, 0 = no better than no change)");
      const bar = el("div", "bar"), fill = el("i");
      const w = v === null || v === undefined ? 0 : Math.max(0, Math.min(1, v));
      fill.style.width = (100 * w) + "%";
      if (v !== null && v !== undefined && v < 0) bar.classList.add("neg");
      bar.append(fill);
      row.append(el("span", "", VARIANT[name]), bar, el("b", "", v === null || v === undefined ? NA : fmt(v, 2)));
      box.append(row);
    });
    return box;
  }
  function rateBar(off, on) {
    // air change on a 0..max scale, fan off vs on
    const max = Math.max(2, (on || 0) * 1.15, (off || 0) * 1.15);
    const box = el("div", "rates");
    [["fan off", off, "off"], ["fan on", on, "on"]].forEach(([label, v, cls]) => {
      const r = el("div", "rt " + cls), bar = el("div", "bar"), fill = el("i");
      fill.style.width = v === null || v === undefined ? "0" : (100 * v / max) + "%";
      bar.append(fill);
      r.append(el("span", "", label), bar, el("b", "", v === null || v === undefined ? "not measured" : fmt(v, 2) + "/h"));
      box.append(r);
    });
    return box;
  }
  function phRender() {
    const d = ph.data;
    if (!d) return;
    $("#phVolume").textContent = "V = " + d.volume_m3 + " m³" + (d.volume_default ? " (placeholder)" : "") + " · forecast " + d.lead + " min";
    $("#phInputs").replaceChildren(...(d.inputs || []).map((i) => {
      const c = el("div", "pin " + i.status.replace(/ /g, "-"));
      c.title = i.detail;
      c.append(el("b", "", i.name), el("span", "term", i.term), el("span", "state", i.status));
      return c;
    }));
    $("#phCards").replaceChildren(...d.pollutants.map((p) => {
      const m = p.model || {}, c = el("section", "card phcard");
      const head = el("div", "head");
      head.append(el("h2", "title", p.label), IAQ.mark(m.used === "no change" || m.used === "paused" ? "warn" : m.used ? "ok" : "off", VARIANT[m.used] || "waiting", VARIANT_HINT[m.used] || "not enough history yet"));
      c.append(head);
      if (m.removal_per_hour === null || m.removal_per_hour === undefined) { c.append(el("div", "empty", "Needs more sensor history")); return c; }
      const facts = el("div", "phfacts");
      facts.append(keyFact("Background", per(m.background, p.unit === "ppm" ? 0 : 1), p.unit, "C_b: level the air relaxes to"),
        keyFact("Decays", String(m.n_decays || 0), m.removal_source === "decay" ? "measured" : "λ assumed", "empty-room periods used for the removal rate"));
      if (m.lambda_online_per_hour !== null && m.lambda_online_per_hour !== undefined) facts.append(keyFact("Live λ", per(m.lambda_online_per_hour, 2) + "/h", "occupied", "air change now, from CO₂ and the camera count"));
      if (m.co2_gen_lpm !== null && m.co2_gen_lpm !== undefined) facts.append(keyFact("CO₂/person", per(m.co2_gen_lpm, 2), "L/min", "≈ 0.3 for seated adults: checks the camera count"));
      c.append(facts, el("div", "label sub", p.kind === "co2" ? "Air change λ" : "Removal: air change + deposition"), rateBar(m.removal_per_hour, m.removal_fan_per_hour),
        el("div", "label sub", "Forecast skill (same test minutes)"), skillBars(p));
      c.addEventListener("click", () => { ph.field = p.key; $("#phField").value = p.key; phChart(); });
      return c;
    }));
    const opts = d.pollutants.map((p) => [p.key, p.label]);
    const sel = $("#phField"), sig = opts.map((o) => o.join("=")).join("|");
    if (sel.dataset.sig !== sig) { sel.dataset.sig = sig; sel.replaceChildren(...opts.map(([v, t]) => new Option(t, v))); }
    if (!ph.field || !opts.some((o) => o[0] === ph.field)) ph.field = (d.pollutants.find((p) => p.kind === "co2") || d.pollutants[0] || {}).key;
    sel.value = ph.field || "";
    phChart();
    phEvents();
    phResearch();
  }
  function phChart() {
    const d = ph.data, host = $("#phChart");
    if (!d || !ph.field) { host.replaceChildren(el("div", "empty", "Unavailable")); return; }
    const p = d.pollutants.find((x) => x.key === ph.field), raw = (d.series || {})[ph.field] || [];
    if (!raw.length) { host.replaceChildren(el("div", "empty", "No physics minutes stored in this range yet (they start with the first model)")); return; }
    const lead = d.lead, byT = {};
    raw.forEach((r) => {
      const row = byT[r.window_start] || (byT[r.window_start] = { t: r.window_start });
      row.value = r.value; row.removal = r.removal_per_hour; row.lambda = r.lambda_online_per_hour;
      row.source = r.source_per_min === null ? null : Math.max(0, r.source_per_min);
      // the forecast made at t is drawn at t + lead, where it can be compared with the reading
      // (server times are handled as UTC by IAQ.ms, so the same UTC fields give them back)
      const tf = new Date(ms(r.window_start) + lead * 60000), p2 = (v) => String(v).padStart(2, "0");
      const k2 = tf.getUTCFullYear() + "-" + p2(tf.getUTCMonth() + 1) + "-" + p2(tf.getUTCDate()) + " " + p2(tf.getUTCHours()) + ":" + p2(tf.getUTCMinutes()) + ":00";
      const fr = byT[k2] || (byT[k2] = { t: k2 });
      fr.fc_phys = r.forecast_physics; fr.fc_used = r.forecast_used;
    });
    let rows = Object.values(byT).sort((a, b) => (a.t < b.t ? -1 : 1));
    const k = Math.ceil(rows.length / 700);
    if (k > 1) rows = rows.filter((_, i) => i % k === 0);
    const unit = p ? p.unit : "";
    IAQ.multiChart(host, { rows: rows, start: rows[0].t, end: rows[rows.length - 1].t, step: k }, [
      { label: p ? p.label : "Value", unit: unit, digits: 1, lines: [{ key: "value", name: "Measured", colour: "var(--text2)" },
        { key: "fc_phys", name: "Physics forecast (" + lead + " min before)", colour: "var(--s1)", dash: true },
        { key: "fc_used", name: "Forecast in use", colour: "var(--s2)", dash: true }] },
      { label: p && p.kind === "co2" ? "Air change" : "Removal rate", unit: "/h", digits: 2, lines: [{ key: "removal", name: "r used", colour: "var(--s3)" },
        { key: "lambda", name: "Live λ (CO₂)", colour: "var(--s1)", dash: true }] },
      { label: "Source S", unit: unit + "/min", digits: 2, lines: [{ key: "source", name: "Added per minute", colour: "var(--s2)" }] },
    ]);
  }
  function phEvents() {
    const evs = (ph.data || {}).events || [], host = $("#phEvents");
    if (!evs.length) { host.replaceChildren(el("li", "empty", "No physics or model events yet: they appear when a removal rate is measured, the forecast model changes, or the room needs more air")); return; }
    host.replaceChildren(...evs.map((e) => {
      const li = el("li", "pev " + (LEVEL[e.level] || ""));
      const pol = (ph.data.pollutants || []).find((p) => p.kind === e.pollutant);
      li.append(el("span", "t", e.ts.slice(5, 16).replace("-", "/")), el("span", "k " + e.kind, e.kind === "model" ? "Model" : "Physics"),
        el("span", "p", pol ? pol.label : (e.pollutant || "")), el("span", "m", e.message));
      return li;
    }));
  }
  function phResearch() {
    const r = (ph.data || {}).research, host = $("#phResearch");
    if (!r) {
      $("#phResWhen").textContent = "";
      const d = el("div", "empty");
      d.append(document.createTextNode("Not run yet. On the PC: "), el("code", "", "python research\\greybox_eval.py --fields co2 pm2_5 pm10"),
        document.createTextNode(" (leave-one-day-out, unseen fan state, forward in time; ridge, BiLSTM, grey-box, physics-informed loss, FiLM)."));
      host.replaceChildren(d);
      return;
    }
    $("#phResWhen").textContent = "run " + r.updated;
    const RUNG = { rung0: "Chronological", rung1: "Leave-one-day-out", rung2: "Unseen fan state", rung4: "Forward in time" };
    const fields = [...new Set(r.rows.map((x) => x.field))], rungs = [...new Set(r.rows.map((x) => x.rung))];
    const models = [...new Set(r.rows.map((x) => x.model))].filter((m) => m !== "persistence");
    const wrap = el("div", "restables");
    fields.forEach((f) => {
      const t = el("table", "restable"), thead = el("thead"), hr = el("tr");
      hr.append(el("th", "", f));
      rungs.forEach((rg) => hr.append(el("th", "", RUNG[rg] || rg)));
      thead.append(hr);
      const tb = el("tbody");
      models.forEach((m) => {
        const tr = el("tr");
        tr.append(el("td", "", m.replace(/_/g, " ")));
        rungs.forEach((rg) => {
          const cellRows = r.rows.filter((x) => x.field === f && x.rung === rg);
          const v = cellRows.find((x) => x.model === m), best = Math.max(...cellRows.filter((x) => x.model !== "persistence").map((x) => x.skill ?? -99));
          const td = el("td", v && v.skill !== null && v.skill === best ? "best" : v && v.skill !== null && v.skill < 0 ? "neg" : "", v && v.skill !== null ? fmt(v.skill, 2) : NA);
          if (v) td.title = "MAE " + fmt(v.mae, 3) + " · " + v.folds + " folds";
          tr.append(td);
        });
        tb.append(tr);
      });
      t.append(thead, tb);
      wrap.append(t);
    });
    host.replaceChildren(el("div", "label", "Skill vs \"no change\" (1 = perfect, 0 = no better, negative = worse); best per column marked."), wrap);
  }
  async function phLoad() {
    try { ph.data = await IAQ.getJSON("/api/physics?" + IAQ.rangeQuery()); } catch (e) { return; }
    phRender();
  }
  $("#phField").addEventListener("change", (e) => { ph.field = e.target.value; phChart(); });
  IAQ.page("physics", {
    enter() { phLoad(); },
    range() { phLoad(); },
    slow() { phLoad(); },
    resize() { phChart(); },
  });

  // ============================================================ EARLY WARNING
  // Pollution in transit: every camera zone is a source with its own travel time to the
  // sensor. What was emitted but has not arrived yet is "committed"; held activity gives the
  // "if work continues" path; the fan-on path shows the effect of acting now.
  const ew = { data: null, field: null, at: 0 };
  const SCEN = { committed: { name: "Already in the air (everyone stops now)", colour: "var(--s3)", dash: "2 4" },
    expected: { name: "Expected (work goes on as usual)", colour: "var(--s2)", dash: "" },
    continue: { name: "Worst case (activity stays as now)", colour: "var(--s2)", dash: "3 3" },
    fan_on: { name: "Expected, fan on now", colour: "var(--s1)", dash: "7 4" } };
  function ewVerdict(it) {
    const lim = it.threshold, t = it.ttt || {}, u = it.unit;
    if (!lim) return { level: "info", text: "Peak in 30 min: " + fmt(it.peak.expected, 1) + " " + u, sub: "no fan threshold set for this sensor" };
    if (it.value >= lim.start) return { level: "bad", text: "Above " + lim.start + " " + u + " now", sub: "the sensor already shows it" };
    const fanTxt = "fan_on" in it.paths ? (t.fan_on ? " · fan on now: in " + t.fan_on + " min" : " · fan on now keeps it below") : "";
    if (t.committed) return { level: "bad", text: "Reaches " + lim.start + " in " + t.committed + " min", sub: "certain: already in the air, even if work stops" + fanTxt };
    if (t.expected) return { level: "warn", text: "Reaches " + lim.start + " in " + t.expected + " min", sub: "expected as work goes on" + fanTxt };
    if (t.continue) return { level: "info", text: "Below " + lim.start + " as expected", sub: "worst case (activity stays as now): " + t.continue + " min" };
    return { level: "ok", text: "Below " + lim.start + " for 30 min", sub: "expected peak " + fmt(it.peak.expected, 1) + " " + u };
  }
  function ewCards() {
    const items = (ew.data || {}).items || [];
    if (!items.length) { $("#ewCards").replaceChildren(el("div", "card empty", "Waiting for the first model: it needs a few hours of camera minutes together with sensor readings")); return; }
    $("#ewCards").replaceChildren(...items.map((it) => {
      const v = ewVerdict(it), c = el("button", "card ewcard " + v.level + (it.field === ew.field ? " sel" : ""));
      c.type = "button";
      const head = el("div", "head");
      head.append(el("h2", "title", it.label), el("b", "now", fmt(it.value, 1) + " " + it.unit));
      c.append(head, el("div", "verdict", v.text), el("div", "label", v.sub),
        el("div", "label", "Already in the air: " + (it.committed_rise >= 0 ? "+" : "") + fmt(it.committed_rise, 1) + " " + it.unit));
      c.addEventListener("click", () => { ew.field = it.field; $("#ewField").value = it.field; ewRender(); });
      return c;
    }));
    const warn = items.filter((it) => it.threshold && it.value < it.threshold.start && ((it.ttt || {}).expected || (it.ttt || {}).committed)).length;
    const b = $("#navWarn"); b.hidden = !warn; b.textContent = warn;
  }
  function ewFlow(it) {
    const host = $("#ewFlow"), cams = ((it.model || {}).cameras || []).slice().sort((a, b) => (a.travel_min ?? 99) - (b.travel_min ?? 99));
    if (!cams.length) { host.replaceChildren(el("div", "empty", "No camera sources yet")); return; }
    const arrive = {}; (it.arriving || []).forEach((a) => { arrive[a.camera] = a.amount; });
    const maxArr = Math.max(1e-9, ...Object.values(arrive));
    const W = 760, bh = 58, gap = 14, H = Math.max(200, 24 + cams.length * (bh + gap) + 24);
    const sx = 470, sy = H / 2, s = IAQ.svg("svg", { viewBox: "0 0 " + W + " " + H, role: "img", "aria-label": "camera zones, travel time to the sensor, forecast" });
    const T = (x, y, txt, o) => IAQ.svg("text", Object.assign({ x: x, y: y, "font-size": 12, fill: "var(--text)" }, o || {}), txt);
    const defs = IAQ.svg("defs");
    const mk = IAQ.svg("marker", { id: "ew-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse" });
    mk.append(IAQ.svg("path", { d: "M0 0L10 5L0 10z", fill: "var(--axis)" }));
    defs.append(mk); s.append(defs);
    cams.forEach((c, i) => {
      const y = 24 + i * (bh + gap), cy = y + bh / 2, a = arrive[c.camera] || 0;
      s.append(IAQ.svg("rect", { x: 16, y: y, width: 250, height: bh, rx: 8, fill: "var(--raise)", stroke: "var(--axis)" }),
        T(28, y + 20, c.name, { "font-weight": 600 }),
        T(28, y + 38, Math.round(100 * c.share) + " % of the source", { "font-size": 11.5, fill: "var(--text2)" }),
        IAQ.svg("rect", { x: 160, y: y + 30, width: 90, height: 8, rx: 4, fill: "var(--grid)" }),
        IAQ.svg("rect", { x: 160, y: y + 30, width: 90 * a / maxArr, height: 8, rx: 4, fill: "var(--s2)" }),
        T(160, y + 52, "arriving: " + fmt(a, 2), { "font-size": 10.5, fill: "var(--muted)" }),
        IAQ.svg("path", { d: "M266 " + cy + " C 360 " + cy + ", 360 " + sy + ", " + (sx - 70) + " " + sy, fill: "none", stroke: "var(--axis)", "stroke-width": 1.25, "marker-end": "url(#ew-arrow)" }),
        T(300, cy - 6 + (cy < sy ? 0 : 14), c.travel_min === null ? "" : "≈ " + fmt(c.travel_min, 1) + " min", { "font-size": 11.5, fill: "var(--text2)" }));
    });
    const v = ewVerdict(it);
    s.append(IAQ.svg("rect", { x: sx - 70, y: sy - 34, width: 140, height: 68, rx: 8, fill: "none", stroke: "var(--axis)" }),
      T(sx, sy - 12, "Sensor now", { "text-anchor": "middle", "font-size": 11.5, fill: "var(--text2)" }),
      T(sx, sy + 14, fmt(it.value, 1) + " " + it.unit, { "text-anchor": "middle", "font-size": 18, "font-weight": 700 }),
      IAQ.svg("path", { d: "M" + (sx + 70) + " " + sy + " H " + (W - 220), fill: "none", stroke: "var(--axis)", "stroke-width": 1.25, "marker-end": "url(#ew-arrow)" }),
      IAQ.svg("rect", { x: W - 216, y: sy - 40, width: 200, height: 80, rx: 8, fill: { ok: "none", bad: "var(--critical)", warn: "var(--warning)", info: "var(--s1)" }[v.level], "fill-opacity": v.level === "ok" ? 1 : 0.15, stroke: { ok: "var(--good)", bad: "var(--critical)", warn: "var(--warning)", info: "var(--s1)" }[v.level], "stroke-width": 2 }),
      T(W - 116, sy - 14, v.text, { "text-anchor": "middle", "font-weight": 700 }),
      T(W - 116, sy + 6, "committed " + (it.committed_rise >= 0 ? "+" : "") + fmt(it.committed_rise, 1) + " " + it.unit, { "text-anchor": "middle", "font-size": 11.5, fill: "var(--text2)" }),
      T(W - 116, sy + 24, "expected peak " + fmt(it.peak.expected, 1), { "text-anchor": "middle", "font-size": 11.5, fill: "var(--text2)" }));
    host.replaceChildren(s);
  }
  function ewChart(it) {
    const host = $("#ewChart"), W = Math.max(320, host.clientWidth || 760), H = 260, L = 48, R = W - 16, T0 = 14, B = H - 28;
    const now = ms(it.time), past = (it.recent || []).map(([t, v]) => [(ms(t) - now) / 60000, v]).filter((p) => p[0] >= -30);
    const keys = Object.keys(it.paths), lim = it.threshold;
    const vals = past.map((p) => p[1]).concat(...keys.map((k) => it.paths[k]), lim ? [lim.start] : [], [it.value]);
    let lo = Math.min(...vals), hi = Math.max(...vals);
    const pad = (hi - lo) * 0.12 || 1; lo -= pad; hi += pad;
    const X = (m) => L + (m + 30) / 60 * (R - L), Y = (v) => B - (v - lo) / (hi - lo) * (B - T0);
    const s = IAQ.svg("svg", { viewBox: "0 0 " + W + " " + H, height: H, role: "img", "aria-label": "measured last 30 minutes and forecast paths for the next 30 minutes" });
    const step = W < 600 ? 20 : 10;
    for (let m = -30; m <= 30; m += step) s.append(IAQ.svg("line", { x1: X(m), x2: X(m), y1: T0, y2: B, stroke: "var(--grid)" }),
      IAQ.svg("text", { x: X(m), y: H - 8, "text-anchor": "middle", "font-size": 11, fill: "var(--muted)" }, m === 0 ? "now" : (m > 0 ? "+" : "") + m + " min"));
    [lo + pad, hi - pad].forEach((v) => s.append(IAQ.svg("text", { x: L - 6, y: Y(v) + 4, "text-anchor": "end", "font-size": 10.5, fill: "var(--muted)" }, fmt(v, 0))));
    s.append(IAQ.svg("rect", { x: X(0), y: T0, width: R - X(0), height: B - T0, fill: "var(--raise)", "fill-opacity": 0.6 }));
    if (lim) s.append(IAQ.svg("line", { x1: L, x2: R, y1: Y(lim.start), y2: Y(lim.start), stroke: "var(--critical)", "stroke-dasharray": "4 3" }),
      IAQ.svg("text", { x: R - 4, y: Y(lim.start) - 6, "text-anchor": "end", "font-size": 11, fill: "var(--critical)" }, "fan start " + lim.start));
    const line = (pts, colour, dash, w) => s.append(IAQ.svg("path", { d: pts.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join(""), fill: "none", stroke: colour, "stroke-width": w || 2, "stroke-dasharray": dash || "" }));
    if (past.length) line(past, "var(--text2)", "", 2.25);
    keys.forEach((k) => line([[0, it.value]].concat(it.paths[k].map((v, i) => [i + 1, v])), SCEN[k].colour, SCEN[k].dash));
    s.append(IAQ.svg("line", { x1: X(0), x2: X(0), y1: T0, y2: B, stroke: "var(--text2)" }));
    host.replaceChildren(s);
    $("#ewLegend").replaceChildren(...[["Measured", "var(--text2)", ""]].concat(keys.map((k) => [SCEN[k].name, SCEN[k].colour, SCEN[k].dash])).map(([n, c, d]) => {
      const k = el("span", "key"), sv = IAQ.svg("svg", { viewBox: "0 0 22 10" });
      sv.append(IAQ.svg("line", { x1: 1, x2: 21, y1: 5, y2: 5, stroke: c, "stroke-width": 2, "stroke-dasharray": d }));
      k.append(sv, document.createTextNode(n)); return k;
    }));
    $("#ewSkill").textContent = it.skill === null || it.skill === undefined ? "" : "10-min skill " + fmt(it.skill, 2) + " · source model R² " + fmt((it.model || {}).r2, 2);
  }
  function ewInv(it) {
    const m = it.model || {}, e = m.emission_per_min || {}, mass = m.emission_mass_per_min || {};
    const massUnit = it.unit === "ppm" ? "mL/min" : (it.unit.indexOf("g/m") >= 0 ? it.unit.replace("/m³", "") + "/min" : "per min × m³");
    const name = { persons: "per person present", motion: "per BH/s of total motion", variance: "per unit of motion variance" };
    const t = el("table"), wrap = el("div", "scroll");
    table(t, ["Activity", "Arrives at the sensor", "Emitted into the room (× " + ((ew.data || {}).volume_m3 || "V") + " m³)"],
      Object.keys(e).map((k) => [name[k] || k, fmt(e[k], 3) + " " + it.unit + "/min", fmt(mass[k], 1) + " " + massUnit]));
    wrap.append(t);
    $("#ewInv").replaceChildren(wrap);
    const note = el("div", "label");
    note.textContent = "Learned without labels: the physics says how much must have arrived each minute; the cameras say who did what, where and when. " + (it.unit === "ppm" ? "Adults at rest exhale about 300 mL CO₂ per minute: compare with 'per person present'." : "");
    $("#ewInv").append(note);
  }
  function ewScore() {
    const sc = (ew.data || {}).score || {};
    $("#ewScore").replaceChildren(keyFact("Warned in time", String(sc.confirmed || 0), "crossings", "the sensor crossed the threshold after an early warning"),
      keyFact("Median lead", sc.median_lead === null || sc.median_lead === undefined ? NA : fmt(sc.median_lead, 0) + " min", "before the sensor", "how much earlier than the sensor"),
      keyFact("Missed", String(sc.missed || 0), "no warning", "the sensor crossed without an early warning"),
      keyFact("False alarms", String(sc.false_alarms || 0), "no crossing", "a warning without a crossing within 40 min"));
    const w = (ew.data || {}).warnings || [];
    $("#ewList").replaceChildren(...(w.length ? w.map((r) => {
      const li = el("li", "pev " + (r.outcome === "missed" ? "bad" : r.outcome === "open" ? "warn" : ""));
      li.append(el("span", "t", (r.warned_at || r.crossed_at || "").slice(5, 16).replace("-", "/")), el("span", "k", r.outcome),
        el("span", "p", POL_NAME[r.pollutant] || r.pollutant || ""),
        el("span", "m", r.outcome === "confirmed" ? "warned " + fmt(r.lead_min, 0) + " min before the sensor" : r.outcome === "missed" ? "crossed without warning" : r.outcome === "open" ? "expected in " + r.ttt_min + " min, peak " + fmt(r.predicted_peak, 1) : "threshold not reached"));
      return li;
    }) : [el("li", "empty", "No warnings yet")]));
  }
  const POL_NAME = { pm1: "PM1", pm2_5: "PM2.5", pm10: "PM10", co2: "CO₂", voc: "VOC" };
  function ewRender() {
    ewCards(); ewScore();
    const items = (ew.data || {}).items || [];
    const opts = items.map((it) => [it.field, it.label]), sel = $("#ewField"), sig = opts.map((o) => o.join("=")).join("|");
    if (sel.dataset.sig !== sig) { sel.dataset.sig = sig; sel.replaceChildren(...opts.map(([v, t]) => new Option(t, v))); }
    if (!items.some((it) => it.field === ew.field)) {
      const urgent = items.filter((it) => (it.ttt || {}).expected).sort((a, b) => a.ttt.expected - b.ttt.expected)[0];
      ew.field = (urgent || items[0] || {}).field;
    }
    sel.value = ew.field || "";
    const it = items.find((x) => x.field === ew.field);
    if (!it) { $("#ewFlow").replaceChildren(); $("#ewChart").replaceChildren(); $("#ewInv").replaceChildren(); return; }
    ewFlow(it); ewChart(it); ewInv(it);
  }
  async function ewLoad() {
    try { ew.data = await IAQ.getJSON("/api/transit"); ew.at = Date.now(); } catch (e) { return; }
    ewRender();
  }
  $("#ewField").addEventListener("change", (e) => { ew.field = e.target.value; ewRender(); });
  IAQ.page("warning", {
    enter() { ewLoad(); },
    slow() { ewLoad(); },
    live() { if (Date.now() - ew.at > 30000) ewLoad(); },
    resize() { ewRender(); },
  });

  // ============================================================ 6 FAN
  const fan = { busy: false, data: null, timer: null, pic: IAQ.FanGraphic($("#fanPic"), "big") };
  async function fanLoad() {
    try { fan.data = await IAQ.getJSON("/api/fan"); } catch (e) { $("#fanResult").textContent = "Status unavailable"; return; }
    fanRender();
  }
  function fact(ic, label, value, cls, title) {
    const d = el("div", "fact" + (cls ? " " + cls : ""));
    if (title) d.title = title;
    const l = el("span", "label"); l.append(icon(ic), document.createTextNode(label));
    d.append(l, el("b", "", value));
    return d;
  }
  function fanRender() {
    const d = fan.data;
    if (!d) return;
    if (!d.enabled) { $("#fanResult").textContent = "Fan control is off (config.yaml)"; $("#fanBtns").hidden = true; $("#fanMode").hidden = true; return; }
    const s = d.status || {}, op = d.operator, manual = s.mode === "manual", g = s.gpio || {};
    fan.pic.set(s);
    $$("#fanMode button").forEach((b) => { b.setAttribute("aria-pressed", b.dataset.mode === s.mode ? "true" : "false"); b.disabled = !op || fan.busy; });
    $("#fanRO").hidden = op;
    $("#fanRO").textContent = "View only";
    $("#fanRO").title = "operators are listed in config.yaml → fan → operators";
    $$("#fanBtns .bigbtn").forEach((b) => {
      b.disabled = !manual || !op || fan.busy;
      b.classList.toggle("active", s.requested === (b.dataset.cmd === "on"));
      b.title = manual ? (b.dataset.cmd === "on" ? "GPIO " + (g.pin ?? 9) + " → relay ON" : "GPIO " + (g.pin ?? 9) + " → relay OFF") : "AI mode: switch to Manual to use";
    });
    // controller choice (fan: controller: dual): which board switches the fan, and its health
    const ctls = s.controllers;
    $("#fanCtl").hidden = !ctls;
    if (ctls) $$("#fanCtl button").forEach((b) => {
      const c = ctls[b.dataset.ctl] || {};
      b.setAttribute("aria-pressed", c.active ? "true" : "false");
      b.disabled = !op || fan.busy || !!c.active;
      b.querySelector(".dot").className = "dot " + (c.ok ? "ok" : "bad");
      b.title = (c.label || b.dataset.ctl) + " " + (c.url || "") + " · " + (c.ok ? "answers" : "not reachable" + (c.error ? ": " + c.error : "")) +
        (c.active ? " · controls the fan now" : " · standby (kept OFF)") + (s.failover === "auto" ? " · automatic switch-over on" : "");
    });
    const lc = s.last_command;
    const pinLevel = s.signal === null || s.signal === undefined ? NA : (s.signal === (g.active_high !== false) ? "HIGH" : "LOW");
    $("#fanFacts").replaceChildren(
      fact("link", s.active === "esp32" ? "ESP32" : "Raspberry Pi", s.ok === false ? "Error" : s.dry_run ? "Dry run" : "Connected", s.ok === false ? "bad" : s.dry_run ? "warn" : "ok", s.ok === false ? s.error : s.controller),
      fact("pin", "GPIO " + (g.pin ?? "?"), pinLevel, "", (s.active === "esp32" ? "ESP32 GPIO " + (g.pin ?? "?") : "BCM " + (g.pin ?? "?") + " (header pin " + (g.pin === 9 ? 21 : "?") + ")") + ", active-" + (g.active_high === false ? "LOW" : "HIGH") + " · level read back from the pin"),
      fact("fan", "Running", s.running === null || s.running === undefined ? "not measured" : s.running ? "Yes" : "No", s.running === null || s.running === undefined ? "muted" : "", "needs a feedback contact or current sensor on the fan board"),
      fact("clock", "Last command", lc ? lc.at.slice(11, 16) + " " + (lc.state || "").toUpperCase() + (lc.ok === false ? " ✕" : " ✓") : NA, lc && lc.ok === false ? "bad" : "", lc ? (lc.reason || "") + (lc.user ? " · " + lc.user : "") + (lc.error ? " · " + lc.error : "") : ""),
      fact("bolt", "Last switch", s.last_change ? s.last_change.slice(11, 16) : NA, "", s.last_change || ""));
    // why
    const dec = s.decision || {};
    $("#fanWhy").textContent = manual ? "Manual mode — the buttons decide." : (dec.stale ? "⚠ " : "") + (dec.reason || NA);
    $("#fanWhen").textContent = dec.at ? dec.at.slice(11) : "";
    const pol = d.policy || {}, rd = dec.readings || {}, fc = dec.forecasts || {};
    const bars = el("div", "polbars");
    Object.keys(pol.pollutants || {}).forEach((k) => {
      const lim = pol.pollutants[k], r = rd[k], f = fc[k];
      const tile = over && over.tiles.find((t) => t.kind === k);
      const value = r ? r[0] : tile ? tile.value : null;
      const forecast = f && f[2] ? f[0] : null;
      const row = el("div", "polbar" + (value !== null && value >= lim.start ? " alarm" : ""));
      row.append(el("span", "pn", tile ? tile.label : k.replace("_", ".")),
        IAQ.bandBar({ value: value, forecast: forecast, fan_limits: lim, bands: tile ? tile.bands : [] }),
        el("b", "", value === null ? NA : String(value)));
      row.title = "start ≥ " + lim.start + " · stop ≤ " + lim.stop + (forecast !== null ? " · forecast " + forecast : "");
      bars.append(row);
    });
    $("#fanBars").replaceChildren(bars);
    const p = el("div", "rules");
    const rule = (ic, label, value, title) => { const r = el("div", "rule"); r.title = title || ""; r.append(icon(ic), el("span", "label", label), el("b", "", value)); return r; };
    p.append(rule("bolt", "Start", "≥ start " + Math.round(pol.activation_seconds / 60 * 10) / 10 + " min", pol.forecast_trigger ? "or " + pol.forecast_confirmations + " valid forecasts in a row ≥ start" : ""),
      rule("check", "Stop", "all ≤ stop " + Math.round(pol.recovery_seconds / 60) + " min", "measured readings only; a forecast never stops the fan"),
      rule("clock", "Min run / pause", Math.round(pol.min_on_seconds / 60) + " / " + Math.round(pol.min_off_seconds / 60) + " min"),
      rule("alert", "Stale data", "fan " + String(pol.stale_action).toUpperCase(), "sensor data older than " + pol.stale_seconds + " s"));
    $("#fanPolicy").replaceChildren(p);
    table($("#fanEvents"), ["Time", "Type", "Fan", "", "By", "Message"],
      (d.events || []).map((e) => [e.ts.slice(5, 16), e.kind.replace("fan_", ""), e.fan_state ? e.fan_state.toUpperCase() : "", e.ok === 1 ? "✓" : e.ok === 0 ? "✕" : "", e.user || (e.kind === "fan_decision" ? "AI" : ""), e.message]));
  }
  async function fanSend(url, body, label) {
    if (fan.busy) return;
    fan.busy = true; fanRender();
    $("#fanResult").className = "result"; $("#fanResult").textContent = label + "…";
    const res = await IAQ.send("POST", url, body);
    fan.busy = false;
    $("#fanResult").className = "result " + (res.ok ? "ok" : "bad");
    $("#fanResult").textContent = res.ok ? "✓ " + (body.state ? label + " accepted" : label) : "✕ " + (res.error || res.message || "failed");
    $("#fanResult").title = res.ok && body.state ? "accepted by the controller; GPIO level read back below — the fan itself is only confirmed with a feedback input" : "";
    await fanLoad(); IAQ.refreshStatus();
  }
  $$("#fanCtl button").forEach((b) => b.addEventListener("click", () => fanSend("/api/fan/controller", { name: b.dataset.ctl }, "Control via " + (b.dataset.ctl === "esp32" ? "ESP32" : "Raspberry Pi"))));
  $$("#fanBtns .bigbtn").forEach((b) => b.addEventListener("click", () => fanSend("/api/fan/command", { state: b.dataset.cmd }, b.dataset.cmd.toUpperCase())));
  $$("#fanMode button").forEach((b) => b.addEventListener("click", () => {
    const s = (fan.data || {}).status || {};
    if (b.dataset.mode === s.mode) return;
    if (b.dataset.mode === "ai" && !confirm("Switch to AI? It decides at once and may switch the fan.")) return;
    fanSend("/api/fan/mode", { mode: b.dataset.mode }, b.dataset.mode === "ai" ? "AI mode" : "Manual mode");
  }));
  IAQ.page("fan", {
    enter() { if (!over) loadOverview(); fanLoad(); clearInterval(fan.timer); fan.timer = setInterval(() => { if (S.page === "fan" && !fan.busy) fanLoad(); }, 3000); },
  });

  // ============================================================ 7 EVENTS
  // Saved events by date: pick a day (or 7 / 30 days / all), newest first, one header per day.
  const ev = { sel: "today" };
  const KIND_NAME = { door: "Door", occupancy: "People", threshold: "Pollutant", sensor: "Sensor", camera: "Camera",
    fan_mode: "Fan mode", fan_command: "Fan command", fan_decision: "Fan AI", fan_error: "Fan error",
    controller: "Fan link", roi: "Area", forecast: "Forecast", system: "System", error: "Error",
    physics: "Physics", model: "Forecast model", warning: "Early warning", experiment: "Experiment" };
  const kindName = (k) => KIND_NAME[k] || k.replace("_", " ");
  const POL_LABEL = { pm1: "PM1", pm2_5: "PM2.5", pm10: "PM10", co2: "CO₂", voc: "VOC" };
  const isoDay = (d) => d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  function evTimeQuery() {
    const today = new Date();
    if (ev.sel === "today") return "day=" + isoDay(today);
    if (ev.sel === "yesterday") { const y = new Date(today); y.setDate(y.getDate() - 1); return "day=" + isoDay(y); }
    if (ev.sel === "7" || ev.sel === "30") return "days=" + ev.sel;
    if (ev.sel === "all") return "";
    return "day=" + ev.sel;                              // a picked date
  }
  function dayTitle(d) {
    const dt = new Date(d + "T12:00:00");
    return dt.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  }
  function markQuick() {
    $$("#evQuick button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.q === ev.sel ? "true" : "false"));
    if (/^\d{4}-/.test(ev.sel)) $("#evDay").value = ev.sel;
  }
  async function evLoad() {
    markQuick();
    const p = new URLSearchParams(evTimeQuery());
    [["kind", "#evKind"], ["level", "#evLevel"], ["camera", "#evCam"], ["pollutant", "#evPol"], ["fan_state", "#evFan"]].forEach(([k, id]) => { if ($(id).value) p.set(k, $(id).value); });
    if ($("#evRoi").value) p.set("roi", $("#evRoi").value);
    if ($("#evQ").value) p.set("q", $("#evQ").value);
    p.set("limit", 2000);
    let d;
    try { d = await IAQ.getJSON("/api/events?" + p.toString()); } catch (e) { $("#evCount").textContent = "unavailable"; return; }
    const ksel = $("#evKind"), keep = ksel.value;
    if (ksel.options.length - 1 !== d.kinds.length) { ksel.replaceChildren(new Option("All types", ""), ...d.kinds.map((k) => new Option(kindName(k), k))); ksel.value = keep; }
    $("#evRange").textContent = d.start ? d.start.slice(0, 16) + " → " + d.end.slice(0, 16) : "all saved events";
    $("#evCount").textContent = d.total + " events" + (d.total > d.events.length ? " (newest " + d.events.length + " shown)" : "");
    // day chips: the days that have saved events
    $("#evDayChips").replaceChildren(...d.days.slice(0, 14).map((x) => {
      const b = el("button", "chip" + (ev.sel === x.day ? " sel" : ""));
      b.type = "button";
      b.append(document.createTextNode(x.day.slice(8, 10) + "/" + x.day.slice(5, 7) + " "), el("b", "", String(x.n)));
      b.title = dayTitle(x.day) + ": " + x.n + " events";
      b.addEventListener("click", () => { ev.sel = x.day; evLoad(); });
      return b;
    }));
    // what happened, by type
    $("#evCounts").replaceChildren(...Object.entries(d.counts).sort((a, b) => b[1] - a[1]).map(([k, n]) => {
      const b = el("button", "chip" + ($("#evKind").value === k ? " sel" : ""));
      b.type = "button";
      b.append(document.createTextNode(kindName(k) + " "), el("b", "", String(n)));
      b.addEventListener("click", () => { $("#evKind").value = $("#evKind").value === k ? "" : k; evLoad(); });
      return b;
    }));
    // table with one header row per day
    const t = $("#evTable"), thead = el("thead"), hr = el("tr"), tbody = el("tbody");
    ["Time", "", "Type", "Cam", "Area", "Pollutant", "Fan", "By", "Message"].forEach((h) => hr.append(el("th", "", h)));
    thead.append(hr);
    let lastDay = null;
    d.events.forEach((e) => {
      const day = e.ts.slice(0, 10);
      if (day !== lastDay) {
        lastDay = day;
        const r = el("tr", "dayrow"), td = el("td", "", dayTitle(day));
        td.colSpan = 9;
        r.append(td);
        tbody.append(r);
      }
      const r = el("tr", "lv-" + (LEVEL[e.level] || "info") + (e.kind === "door" ? " door" : ""));
      const lv = dot(LEVEL[e.level] === "bad" ? "bad" : LEVEL[e.level] === "warn" ? "warn" : "ok", e.level || "info");
      const msg = el("span", "", e.message);
      if (e.details) msg.title = e.details;
      [e.ts.slice(11, 19), lv, kindName(e.kind), e.camera ?? "", e.roi || "", POL_LABEL[e.pollutant] || e.pollutant || "", e.fan_state ? e.fan_state.toUpperCase() : "", e.user || "", msg]
        .forEach((c) => { const td = el("td"); if (c instanceof Node) td.append(c); else td.textContent = c; r.append(td); });
      tbody.append(r);
    });
    if (!d.events.length) { const r = el("tr"), td = el("td", "empty", "No saved events for this selection"); td.colSpan = 9; r.append(td); tbody.append(r); }
    t.replaceChildren(thead, tbody);
    $("#evCsv").href = "/export/events.csv?" + (d.start ? "from=" + d.start.slice(0, 16).replace(" ", "T") + "&to=" + d.end.slice(0, 16).replace(" ", "T") : "minutes=" + 7 * 1440);
  }
  $$("#evQuick button").forEach((b) => b.addEventListener("click", () => { ev.sel = b.dataset.q; $("#evDay").value = ""; evLoad(); }));
  $("#evDay").addEventListener("change", (e) => { if (e.target.value) { ev.sel = e.target.value; evLoad(); } });
  $("#evForm").addEventListener("submit", (e) => { e.preventDefault(); evLoad(); });
  // Experiment log: start / stop a labelled test (operators), shown above the event table.
  const exp = { data: null };
  async function expLoad() {
    let d;
    try { d = await IAQ.getJSON("/api/experiments"); } catch (e) { $("#expNow").textContent = "Experiment log unavailable"; return; }
    exp.data = d;
    const sel = $("#expType");
    if (!sel.options.length) sel.replaceChildren(...Object.entries(d.types).map(([k, v]) => new Option(v, k)));
    const a = d.active;
    $("#expNow").className = "expnow" + (a ? " on" : "");
    $("#expNow").textContent = a ? "● Running: " + (d.types[a.type] || a.type) + " since " + a.start.slice(11, 16) + (a.note ? " — " + a.note : "") + (a.user ? " (" + a.user + ")" : "")
      : "No experiment running" + (d.operator ? "" : " · only operators can start one");
    ["#expStart", "#expStop", "#expAddNote", "#expType", "#expNote"].forEach((q) => { $(q).disabled = !d.operator; });
    $("#expStart").disabled = !d.operator || !!a; $("#expStop").disabled = !d.operator || !a;
    table($("#expTable"), ["Time", "By", "What"], (d.events || []).slice(0, 30).map((e) => [e.ts.slice(5, 16), e.user || "", e.message]));
  }
  async function expSend(body, label) {
    $("#expResult").className = "result"; $("#expResult").textContent = label + "…";
    const res = await IAQ.send("POST", "/api/experiments", body);
    $("#expResult").className = "result " + (res.ok ? "ok" : "bad");
    $("#expResult").textContent = res.ok ? "✓ " + label : "✕ " + (res.error || "failed");
    if (res.ok) $("#expNote").value = "";
    await expLoad(); evLoad();
  }
  $("#expStart").addEventListener("click", () => expSend({ action: "start", type: $("#expType").value, note: $("#expNote").value }, "started"));
  $("#expStop").addEventListener("click", () => expSend({ action: "stop", note: $("#expNote").value }, "stopped"));
  $("#expAddNote").addEventListener("click", () => expSend({ action: "note", note: $("#expNote").value }, "note saved"));

  IAQ.page("events", {
    enter() { evLoad(); expLoad(); },
    slow() { if (ev.sel === "today" || ev.sel === "7" || ev.sel === "30" || ev.sel === "all") evLoad(); },
    live(live) {
      const sel = $("#evCam"), sig = live.cameras.map((c) => c.id).join(",");
      if (sel.dataset.sig !== sig) { sel.dataset.sig = sig; const v = sel.value; sel.replaceChildren(new Option("All cameras", ""), ...live.cameras.map((c) => new Option("C" + c.id + " " + (c.location || ""), c.id))); sel.value = v; }
    },
  });

  IAQ.start();
})();
