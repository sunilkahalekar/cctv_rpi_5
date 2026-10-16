/* Small visual building blocks: icons, the exhaust-fan picture, gauge ring, band bar. */
(function () {
  "use strict";
  const { el, svg } = IAQ;

  // ------------------------------------------------------------------ icons
  // 24x24 line icons (stroke = currentColor)
  const PATHS = {
    person: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21c0-4.4 3.6-7 8-7s8 2.6 8 7",
    run: "M14 4.5a1.8 1.8 0 1 0 0-.1M10 21l2.5-6 2.5 2v5M7 12l3-3.5 4 1 2.5 3.5 2.5.5M9.5 8.5 8 14l-4 1.5",
    wave: "M2 12c2-4 4-4 6 0s4 4 6 0 4-4 6 0 2 3 2 3",
    spread: "M3 18h18M6 18V9M10 18V5M14 18v-6M18 18v-9",
    gauge: "M4 17a8 8 0 1 1 16 0M12 17l4-5",
    camera: "M3 7h4l2-2h6l2 2h4v12H3zM12 17a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
    sensor: "M12 3v4M5.6 5.6l2.8 2.8M3 12h4M18.4 5.6l-2.8 2.8M21 12h-4M7 20h10l-1-6H8z",
    fan: "M12 12m-2 0a2 2 0 1 0 4 0 2 2 0 1 0-4 0M12 10c0-4 1-7 4-7 2 0 3 2 1 4l-3 3M14 12c4 0 7 1 7 4 0 2-2 3-4 1l-3-3M12 14c0 4-1 7-4 7-2 0-3-2-1-4l3-3M10 12c-4 0-7-1-7-4 0-2 2-3 4-1l3 3",
    cpu: "M7 7h10v10H7zM10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4",
    alert: "M12 3 2 20h20zM12 10v4M12 17.5v.5",
    check: "M5 12.5 10 17 19 7",
    link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
    bolt: "M13 2 4 14h7l-1 8 9-12h-7z",
    clock: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2",
    pin: "M9 3h6v4l2 3v2H7v-2l2-3zM12 12v9",
    info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v5M12 7.5v.5",
    door: "M5 21V3h11v18M16 7l4-1v15h-4M13 12v1",
    area: "M4 6l7-3 9 4-2 12-11 2z",
  };
  IAQ.icon = function (name, cls) {
    const s = svg("svg", { viewBox: "0 0 24 24", class: "icon" + (cls ? " " + cls : ""), "aria-hidden": "true" });
    s.append(svg("path", { d: PATHS[name] || PATHS.info, fill: "none", stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round", "stroke-linejoin": "round" }));
    return s;
  };

  // --------------------------------------------------- the exhaust fan picture
  // A wall exhaust fan with a digital panel. Blades turn when the relay is ON, airflow
  // lines appear; OFF = still with red LED; unknown = amber LED, dimmed.
  IAQ.FanGraphic = function (host, size) {
    const big = size !== "small";
    const wrap = el("div", "fanpic" + (big ? "" : " small"));
    const s = svg("svg", { viewBox: "0 0 300 240", role: "img", "aria-label": "exhaust fan" });
    const defs = svg("defs");
    const lin = (id, stops, x2, y2) => {
      const g = svg("linearGradient", { id: id, x1: 0, y1: 0, x2: x2 === undefined ? 0 : x2, y2: y2 === undefined ? 1 : y2 });
      stops.forEach(([o, c]) => g.append(svg("stop", { offset: o, "stop-color": c })));
      defs.append(g);
    };
    const uid = "f" + Math.random().toString(36).slice(2, 7);
    lin(uid + "h", [[0, "#f4f5f7"], [0.5, "#d9dce1"], [1, "#b8bcc4"]], 1, 1);
    lin(uid + "b", [[0, "#5b6573"], [1, "#2f3640"]], 1, 1);
    const rg = svg("radialGradient", { id: uid + "s", cx: 0.5, cy: 0.5, r: 0.5 });
    rg.append(svg("stop", { offset: 0, "stop-color": "#2b3038" }), svg("stop", { offset: 1, "stop-color": "#0f1216" }));
    defs.append(rg);
    s.append(defs);
    // airflow (behind)
    const air = svg("g", { class: "air" });
    [[70, 0], [120, 1], [170, 2]].forEach(([y, i]) => air.append(svg("path", {
      d: `M232 ${y} C 252 ${y - 10}, 268 ${y + 10}, 292 ${y}`, fill: "none", stroke: "var(--s1)", "stroke-width": 4, "stroke-linecap": "round", class: "flow f" + i })));
    s.append(air);
    // housing
    s.append(svg("rect", { x: 6, y: 6, width: 228, height: 228, rx: 24, fill: `url(#${uid}h)`, stroke: "#8a8f98", "stroke-width": 2 }),
      svg("rect", { x: 18, y: 18, width: 204, height: 204, rx: 16, fill: "none", stroke: "#ffffff", "stroke-opacity": 0.7, "stroke-width": 2 }));
    [[24, 24], [216, 24], [24, 216], [216, 216]].forEach(([x, y]) => s.append(svg("circle", { cx: x, cy: y, r: 4, fill: "#9aa0a8", stroke: "#6d727a" })));
    s.append(svg("circle", { cx: 120, cy: 120, r: 98, fill: "#c3c7cd", stroke: "#8a8f98", "stroke-width": 2 }),
      svg("circle", { cx: 120, cy: 120, r: 92, fill: `url(#${uid}s)` }));
    // blades
    const blades = svg("g", { class: "blades" });
    for (let k = 0; k < 5; k++) {
      blades.append(svg("path", { d: "M120 120 C 103 100, 98 62, 112 30 C 140 30, 156 64, 134 110 Z", fill: `url(#${uid}b)`, stroke: "#20252c", "stroke-width": 1, transform: `rotate(${k * 72} 120 120)` }));
    }
    blades.append(svg("circle", { cx: 120, cy: 120, r: 20, fill: "#3b424d", stroke: "#1b1f25", "stroke-width": 2 }),
      svg("circle", { cx: 120, cy: 120, r: 7, fill: "#9aa3ad" }));
    s.append(blades);
    // grille in front
    const grille = svg("g", { fill: "none", stroke: "#d8dce2", "stroke-opacity": 0.55, "stroke-width": 1.6 });
    [38, 60, 82].forEach((r) => grille.append(svg("circle", { cx: 120, cy: 120, r: r })));
    [0, 45, 90, 135].forEach((a) => grille.append(svg("line", { x1: 120, y1: 30, x2: 120, y2: 210, transform: `rotate(${a} 120 120)` })));
    s.append(grille);
    const led = svg("circle", { cx: 206, cy: 206, r: 6, class: "led" });
    s.append(led);
    // digital panel
    const panel = el("div", "lcd");
    const big1 = el("span", "lcd-state", "--"), mode = el("span", "lcd-mode", ""), tag = el("span", "lcd-tag", "");
    panel.append(big1, mode, tag);
    wrap.append(s, panel);
    host.replaceChildren(wrap);
    return {
      set(st) {
        const on = st.requested === true, off = st.requested === false;
        wrap.classList.toggle("on", on);
        wrap.classList.toggle("off", off);
        wrap.classList.toggle("unknown", !on && !off);
        wrap.classList.toggle("fault", st.ok === false);
        big1.textContent = on ? "ON" : off ? "OFF" : "– –";
        mode.textContent = st.mode === "ai" ? "AUTO · AI" : st.mode === "manual" ? "MANUAL" : "";
        tag.textContent = st.ok === false ? "LINK ERROR" : st.dry_run ? "DRY RUN" : "";
        s.setAttribute("aria-label", "exhaust fan " + big1.textContent + ", " + mode.textContent);
      },
    };
  };

  // ---------------------------------------------------------------- ring gauge
  IAQ.ring = function (value, max, label) {
    const s = svg("svg", { viewBox: "0 0 64 64", class: "ring", role: "img", "aria-label": label || "" });
    const r = 26, c = 2 * Math.PI * r, f = value === null || value === undefined ? 0 : Math.max(0, Math.min(1, value / (max || 100)));
    s.append(svg("circle", { cx: 32, cy: 32, r: r, fill: "none", stroke: "var(--grid)", "stroke-width": 7 }),
      svg("circle", { cx: 32, cy: 32, r: r, fill: "none", stroke: "var(--s2)", "stroke-width": 7, "stroke-linecap": "round",
        "stroke-dasharray": (c * f).toFixed(1) + " " + c.toFixed(1), transform: "rotate(-90 32 32)" }),
      svg("text", { x: 32, y: 37, "text-anchor": "middle", "font-size": 16, "font-weight": 700, fill: "var(--text)" }, value === null || value === undefined ? "–" : Math.round(value)));
    return s;
  };

  // ------------------------------------------------------------- band bar
  // Category bands as coloured segments, the reading as a filled dot, the forecast as a
  // ring, the fan start (solid) / stop (dashed) thresholds as ticks. Plain HTML so it
  // stretches to any width.
  IAQ.bandBar = function (t) {
    const bands = (t.bands || []).slice().sort((a, b) => a.value - b.value);
    const vals = [t.value, t.forecast, t.fan_limits && t.fan_limits.start].filter((v) => v !== null && v !== undefined);
    const top = Math.max(bands.length ? bands[Math.min(bands.length - 1, 1)].value * 1.4 : 0, ...vals.map((v) => v * 1.15), 1);
    const P = (v) => Math.max(0, Math.min(100, (v / top) * 100)) + "%";
    const bar = el("div", "bandbar");
    const track = el("div", "track");
    const edges = bands.filter((b) => b.value < top);
    let from = 0;
    [{ value: 0, level: 0 }].concat(edges).forEach((b, i) => {
      const to = i < edges.length ? edges[i].value : top;
      const seg = el("i", "l" + Math.min(3, i === 0 ? 0 : edges[i - 1].level));
      seg.style.left = P(from); seg.style.width = "calc(" + ((to - from) / top) * 100 + "% - 2px)";
      track.append(seg);
      from = to;
    });
    bar.append(track);
    if (t.fan_limits) ["start", "stop"].forEach((k) => {
      const m = el("span", "tick " + k); m.style.left = P(t.fan_limits[k]); m.title = "fan " + k + " " + t.fan_limits[k]; bar.append(m);
    });
    if (t.forecast !== null && t.forecast !== undefined) { const m = el("span", "mk fc"); m.style.left = P(t.forecast); m.title = "forecast " + t.forecast; bar.append(m); }
    if (t.value !== null && t.value !== undefined) { const m = el("span", "mk now"); m.style.left = P(t.value); m.title = "now " + t.value; bar.append(m); }
    return bar;
  };
})();
