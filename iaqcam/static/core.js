/* Shared helpers, data store, page navigation, time range, header status. */
(function () {
  "use strict";
  const IAQ = (window.IAQ = {});
  const $ = (IAQ.$ = (sel, root) => (root || document).querySelector(sel));
  IAQ.$$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const NS = "http://www.w3.org/2000/svg";
  IAQ.showVideo = document.body.dataset.showVideo === "true";
  IAQ.ICON = ["✓", "!", "▲", "✕"];

  const pad = (n) => String(n).padStart(2, "0");
  // Server times are local-time strings; they are handled as if they were UTC so that the
  // browser's own time zone never shifts them.
  IAQ.ms = function (text) {
    const m = /(\d+)-(\d+)-(\d+)[ T](\d+):(\d+)(?::(\d+))?/.exec(text || "");
    return m ? Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) : null;
  };
  IAQ.clock = function (t, seconds) {
    const d = new Date(t);
    return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + (seconds ? ":" + pad(d.getUTCSeconds()) : "");
  };
  IAQ.dayClock = function (t) {
    const d = new Date(t);
    return d.getUTCDate() + " " + ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][d.getUTCMonth()] + " " + IAQ.clock(t);
  };
  IAQ.fmt = function (v, digits, missing) {
    if (v === null || v === undefined || Number.isNaN(v)) return missing === undefined ? "–" : missing;
    return Number(v).toFixed(digits === undefined ? 1 : digits);
  };
  IAQ.sci = function (v, missing) {          // small variances: keep them readable
    if (v === null || v === undefined || Number.isNaN(v)) return missing === undefined ? "–" : missing;
    if (v === 0) return "0";
    return Math.abs(v) >= 0.01 ? Number(v).toFixed(3) : Number(v).toExponential(2);
  };
  IAQ.signed = function (v, digits) {
    if (v === null || v === undefined) return "–";
    const s = Math.abs(v).toFixed(digits);
    return (v > 0 ? "+" : v < 0 ? "−" : "") + s;
  };
  IAQ.el = function (tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  };
  IAQ.svg = function (tag, attrs, text) {
    const n = document.createElementNS(NS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
    if (text !== undefined) n.textContent = text;
    return n;
  };
  IAQ.table = function (node, head, body) {
    const thead = IAQ.el("thead"), tr = IAQ.el("tr"), tbody = IAQ.el("tbody");
    head.forEach((h) => tr.append(IAQ.el("th", "", h)));
    thead.append(tr);
    body.forEach((cells) => {
      const r = IAQ.el("tr");
      cells.forEach((c) => { const td = IAQ.el("td"); if (c instanceof Node) td.append(c); else td.textContent = c; r.append(td); });
      tbody.append(r);
    });
    if (!body.length) { const r = IAQ.el("tr"), td = IAQ.el("td", "empty", "Nothing to show"); td.colSpan = head.length; r.append(td); tbody.append(r); }
    node.replaceChildren(thead, tbody);
  };
  IAQ.chip = function (text, value, title, cls) {
    const c = IAQ.el("span", "chip" + (cls ? " " + cls : ""));
    if (cls === "in" || cls === "out") c.append(IAQ.el("i"));
    c.append(document.createTextNode(text + (value !== undefined ? " " : "")));
    if (value !== undefined) c.append(IAQ.el("b", "", value));
    if (title) c.title = title;
    return c;
  };
  IAQ.status = function (band) {
    const s = IAQ.el("span", "st" + (band ? " l" + band.level : ""));
    s.append(IAQ.el("span", "ic", band ? IAQ.ICON[band.level] : "·"), IAQ.el("span", "", band ? band.label : "Unavailable"));
    return s;
  };
  IAQ.mark = function (kind, text, title) {        // ok / warn / bad / off / info word marks
    const s = IAQ.el("span", "mark " + kind, text);
    if (title) s.title = title;
    return s;
  };
  IAQ.cameraLabel = (c) => (c.location ? c.location + " (" + c.name + ")" : c.name);

  // ------------------------------------------------------------------ requests
  IAQ.getJSON = async function (url) {
    const r = await fetch(url, { cache: "no-store" });
    if (r.status === 401) { window.location.href = "/login"; throw new Error("signed out"); }
    if (!r.ok) throw new Error(url + " -> " + r.status);
    return r.json();
  };
  IAQ.send = async function (method, url, body) {
    try {
      const r = await fetch(url, { method: method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
      if (r.status === 401) { window.location.href = "/login"; return { ok: false, error: "Signed out" }; }
      const data = await r.json().catch(() => ({}));
      if (!r.ok && data.ok === undefined) data.ok = false;
      if (!r.ok && !data.error) data.error = data.message || ("request failed (" + r.status + ")");
      return data;
    } catch (e) { return { ok: false, error: "The app cannot be reached: " + e.message }; }
  };

  // -------------------------------------------------------------- shared state
  IAQ.state = { minutes: 60, rec: null, live: null, page: "overview", cam: null, field: null, scope: "inside" };
  try { IAQ.state.cam = Number(localStorage.getItem("cam")) || null; } catch (e) { /* private window */ }
  IAQ.rangeQuery = function () {
    const s = IAQ.state;
    return s.rec ? "from=" + encodeURIComponent(s.rec.from) + "&to=" + encodeURIComponent(s.rec.to) : "minutes=" + s.minutes;
  };
  IAQ.selectCamera = function (id) {
    IAQ.state.cam = id;
    try { localStorage.setItem("cam", id); } catch (e) { /* ignore */ }
  };

  // ----------------------------------------------------------- page handlers
  const pages = {};               // name -> {enter, live, range, timers}
  IAQ.page = function (name, handlers) { pages[name] = handlers; };
  function route() {
    const hash = (location.hash || "#overview").slice(1);
    const [name, query] = hash.split("?");
    const page = pages[name] ? name : "overview";
    IAQ.state.page = page;
    IAQ.state.params = new URLSearchParams(query || "");
    IAQ.$$("main.page").forEach((m) => { m.hidden = m.dataset.page !== page; });
    IAQ.$$("#tabs a").forEach((a) => a.setAttribute("aria-current", a.dataset.page === page ? "page" : "false"));
    $("#rangeBar").hidden = !["overview", "cameras", "analytics", "air", "physics"].includes(page);
    if (pages[page].enter) pages[page].enter(IAQ.state.params);
    if (IAQ.state.live && pages[page].live) pages[page].live(IAQ.state.live);
  }
  window.addEventListener("hashchange", route);
  IAQ.go = (hash) => { if (location.hash === hash) route(); else location.hash = hash; };
  IAQ.current = () => pages[IAQ.state.page] || {};

  // ------------------------------------------------------------ header pills
  function pill(id, cls, text, title) {
    const p = $(id);
    p.className = "pill " + cls;
    p.lastElementChild.textContent = text;
    p.title = title || "";
  }
  IAQ.pill = pill;
  function renderHeader(live, status) {
    if (live) {
      const n = live.cameras.length, on = live.cameras_online;
      const off = live.cameras.filter((c) => !c.online).map((c) => c.name).join(", ");
      pill("#hCam", on === n ? "on" : on ? "warn" : "off", "Cameras " + on + "/" + n, off ? "Offline: " + off : "all camera streams deliver pictures");
      $("#clock").textContent = live.now.slice(11);
      const info = live.info || {};
      $("#foot").textContent = (info.model ? info.model.replace(".pt", "") + " · " + info.imgsz + " px · " + info.device : "")
        + (info.urls && info.urls.length ? " · " + info.urls.join(" · ") : "");
      $("#foot").title = "started " + (info.started || "");
    }
    if (status) {
      const s = status.sensors;
      if (s.age === null) pill("#hSen", "off", "Sensors –", s.error || "no reading");
      else if (s.age <= s.stale_after) pill("#hSen", "on", "Sensors " + s.last.slice(11, 16), "last reading " + s.last + (s.channel ? " (" + s.channel + ")" : ""));
      else pill("#hSen", s.age < 3 * s.stale_after ? "warn" : "off", "Sensors stale", s.error || "last reading " + s.last);
      const fs = status.health.forecast || "";
      pill("#hFor", /fail/i.test(fs) ? "off" : /Collecting|Waiting/i.test(fs) ? "warn" : "on", /Collecting|Waiting/i.test(fs) ? "Forecast learning" : /fail/i.test(fs) ? "Forecast error" : "Forecast", fs);
      const f = status.fan;
      if (!f) pill("#hFan", "", "Fan –", "fan control is off in config.yaml");
      else pill("#hFan", f.ok === false ? "off" : f.dry_run ? "warn" : "on",
        "Fan " + (f.requested === true ? "ON" : f.requested === false ? "OFF" : "?") + " · " + (f.mode === "ai" ? "AI" : "Manual"),
        (f.ok === false ? "controller error: " + (f.error || "") : (f.controller || "")) + (f.dry_run ? " (dry run)" : ""));
    }
  }
  IAQ.renderHeader = renderHeader;

  // ------------------------------------------------------------ time range
  IAQ.$$("#range button").forEach((b) => b.addEventListener("click", () => { IAQ.state.minutes = +b.dataset.m; leaveRecorded(); }));
  function markRange() {
    const s = IAQ.state;
    IAQ.$$("#range button").forEach((x) => x.setAttribute("aria-pressed", !s.rec && +x.dataset.m === s.minutes ? "true" : "false"));
    $("#rec").hidden = !s.rec;
    if (s.rec) $("#recText").textContent = IAQ.dayClock(IAQ.ms(s.rec.from)) + " → " + IAQ.dayClock(IAQ.ms(s.rec.to));
    IAQ.$$("#csvLinks a").forEach((a) => { a.href = "/export/" + a.dataset.export + ".csv?" + IAQ.rangeQuery(); });
  }
  function rangeChanged() { markRange(); const p = IAQ.current(); if (p.range) p.range(); }
  function leaveRecorded() { IAQ.state.rec = null; $("#whenNote").textContent = ""; rangeChanged(); }
  $("#recLive").addEventListener("click", leaveRecorded);
  $("#when").addEventListener("submit", (e) => {
    e.preventDefault();
    const from = $("#whenFrom").value, to = $("#whenTo").value;
    if (!from || !to || to <= from) { $("#whenNote").textContent = "The end must be after the start."; return; }
    if (IAQ.ms(to) - IAQ.ms(from) > 31 * 86400000) { $("#whenNote").textContent = "At most 31 days."; return; }
    $("#whenNote").textContent = "";
    IAQ.state.rec = { from: from, to: to };
    rangeChanged();
  });

  // ------------------------------------------------------------------ theme
  $("#theme").addEventListener("click", () => {
    const root = document.documentElement;
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
    root.dataset.theme = dark ? "light" : "dark";
    try { localStorage.setItem("theme", root.dataset.theme); } catch (e) { /* private window */ }
    const p = IAQ.current(); if (p.resize) p.resize();
  });
  try { const saved = localStorage.getItem("theme"); if (saved) document.documentElement.dataset.theme = saved; } catch (e) { /* ignore */ }

  // ------------------------------------------------------------ live loop
  IAQ.status_ = null;
  async function refreshLive() {
    try { IAQ.state.live = await IAQ.getJSON("/api/live"); $("#fault").hidden = true; }
    catch (e) { $("#fault").hidden = false; $("#faultText").textContent = "Last try: " + new Date().toLocaleTimeString() + ". " + e.message; pill("#hCam", "off", "App not reachable"); return; }
    renderHeader(IAQ.state.live, null);
    if (IAQ.setupBadge) IAQ.setupBadge(IAQ.state.live);
    const p = IAQ.current();
    if (p.live) p.live(IAQ.state.live);
    if (IAQ.onLive) IAQ.onLive(IAQ.state.live);
  }
  async function refreshStatus() {
    try { IAQ.status_ = await IAQ.getJSON("/api/status"); } catch (e) { return; }
    renderHeader(null, IAQ.status_);
    const p = IAQ.current();
    if (p.status) p.status(IAQ.status_);
  }
  IAQ.refreshStatus = refreshStatus;
  let resizeTimer = null;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { const p = IAQ.current(); if (p.resize) p.resize(); }, 150); });

  IAQ.start = function () {
    markRange();
    route();
    refreshLive();
    refreshStatus();
    setInterval(refreshLive, 2000);
    setInterval(refreshStatus, 5000);
    setInterval(() => { const p = IAQ.current(); if (p.slow && !IAQ.state.rec) p.slow(); }, 30000);
  };
})();
