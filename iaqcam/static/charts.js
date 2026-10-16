/* Charts: pollutant chart with camera strips, small-multiple time charts, sparklines. */
(function () {
  "use strict";
  const { el, svg, fmt, sci, ms, clock, dayClock } = IAQ;

  function niceMax(v) {
    if (!(v > 0)) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v))), n = v / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 4 ? 4 : n <= 5 ? 5 : n <= 8 ? 8 : 10) * p;
  }
  function tickMinutes(span, maxTicks) {
    for (const s of [5, 10, 15, 30, 60, 120, 180, 360, 720, 1440, 2880, 5760]) if (span / s <= maxTicks) return s;
    return 11520;
  }
  function segments(rows, key, stepMs) {
    const segs = [];
    let cur = [];
    rows.forEach((r) => {
      if (r[key] === undefined || r[key] === null) return;
      if (cur.length && r.x - cur[cur.length - 1].x > 2.6 * stepMs) { segs.push(cur); cur = []; }
      cur.push(r);
    });
    if (cur.length) segs.push(cur);
    return segs;
  }
  IAQ.niceMax = niceMax;
  function legendKey(inner, text, title) {
    const k = el("span", "key"), s = svg("svg", { viewBox: "0 0 22 10" });
    s.append(inner);
    k.append(s, document.createTextNode(text));
    if (title) k.title = title;
    return k;
  }
  function tipLine(colour, dash, name, value) {
    const r = el("div", "r"), k = el("span", "k");
    k.style.borderTop = "2px " + (dash ? "dashed " : "solid ") + colour;
    r.append(k, el("span", "", name), el("b", "", value));
    return r;
  }
  function placeTip(tip, s, W, x) {
    const box = s.getBoundingClientRect();
    tip.style.display = "block";
    const left = x * (box.width / W);
    tip.style.left = Math.min(Math.max(0, left + 14), Math.max(0, box.width - tip.offsetWidth - 4)) + "px";
    tip.style.top = "14px";
  }

  // ------------------------------------------------------------ sparkline
  IAQ.spark = function (points, forecast, lead) {
    const W = 222, H = 40, s = svg("svg", { class: "spark", viewBox: "0 0 " + W + " " + H, preserveAspectRatio: "none", "aria-hidden": "true" });
    const pts = points.map(([t, v]) => [ms(t), v]), fc = (forecast || []).map(([t, v]) => [ms(t), v]);
    if (!pts.length) return s;
    const all = pts.concat(fc), t1 = Math.max(...all.map((p) => p[0])), t0 = t1 - (60 + (lead || 10)) * 60000;
    let lo = Math.min(...all.map((p) => p[1])), hi = Math.max(...all.map((p) => p[1]));
    if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
    const X = (t) => 3.5 + (t - t0) / (t1 - t0) * (W - 7), Y = (v) => 5 + (1 - (v - lo) / (hi - lo)) * (H - 10);
    const path = (arr) => arr.filter((p) => p[0] >= t0).map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join("");
    // stretched to the tile width: strokes stay 2 px (non-scaling), no end dot (it would turn oval)
    s.append(svg("path", { d: path(pts), fill: "none", stroke: "var(--s1)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke" }));
    const last = pts[pts.length - 1];
    if (fc.length) s.append(svg("path", { d: path([last].concat(fc)), fill: "none", stroke: "var(--s2)", "stroke-width": 2, "stroke-dasharray": "4 3", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke" }));
    return s;
  };

  // ------------------------------------------------- pollutant chart + strips
  IAQ.PollutantChart = function (section) {
    const found = (c) => section.querySelector(c);
    const dummy = document.createElement("span");
    const q = (c) => found(c) || dummy;
    const host = q(".js-chart"), tip = el("div", "tip");
    let data = null;
    function render() {
      const d = data;
      if (!d || !d.field) { host.replaceChildren(el("div", "empty", "No sensor reading is stored yet: chart unavailable."), tip); return; }
      const f = d.field, lead = d.lead, live = IAQ.state;
      q(".js-ctitle").textContent = f.label + " (" + f.unit + ")";
      q(".js-csub").textContent = f.unit + " · " + (d.live ? "last " + (live.minutes >= 60 ? live.minutes / 60 + " h" : live.minutes + " min") + " + next " + lead + " min"
        : dayClock(ms(d.start)) + " to " + dayClock(ms(d.end))) + " · people and motion: " + d.scope_label;
      q(".js-badge").textContent = d.note.badge;
      q(".js-badge").className = "badge js-badge " + d.note.level;
      q(".js-mtext").textContent = d.note.text;
      const items = [
        legendKey(svg("line", { x1: 1, x2: 21, y1: 5, y2: 5, stroke: "var(--s1)", "stroke-width": 2 }), "Measured"),
        legendKey(svg("line", { x1: 1, x2: 21, y1: 5, y2: 5, stroke: "var(--s2)", "stroke-width": 2, "stroke-dasharray": "6 4" }), "Forecast +" + lead + " min", "made " + lead + " minutes earlier, drawn at the time it is for"),
        legendKey(svg("rect", { x: 1, y: 1, width: 20, height: 8, fill: "var(--ctx)", rx: 2 }), "People & motion"),
      ];
      if (d.rows.some((r) => r.n_in !== undefined)) items.push(legendKey(svg("rect", { x: 1, y: 0, width: 20, height: 5, fill: "var(--s3)" }), "Door in"), legendKey(svg("rect", { x: 1, y: 5, width: 20, height: 5, fill: "var(--ctx)" }), "Door out"));
      q(".js-legend").replaceChildren(...items);

      const W = Math.max(320, host.clientWidth || 800), narrow = W < 600;
      const L = 96, R = W - (narrow ? 70 : 164), TOP = 10, BOT = 270, H = 577;
      const stepMs = d.step * 60000, t0 = ms(d.start), tEnd = ms(d.end), t1 = tEnd + lead * 60000;
      const rows = d.rows.map((r) => Object.assign({ x: ms(r.t) }, r));
      const X = (t) => L + 6 + (t - t0) / (t1 - t0) * (R - L - 12);
      const vals = rows.flatMap((r) => [r.measured, r.forecast]).filter((v) => v !== undefined);
      const ymax = niceMax(Math.max(1e-9, ...vals) * 1.06);
      const Y = (v) => BOT - (v / ymax) * (BOT - TOP - 16);
      const s = svg("svg", { role: "img", "aria-label": f.label + " chart", viewBox: "0 0 " + W + " " + H, height: H });
      s.append(svg("text", { x: 12, y: (TOP + BOT) / 2, transform: "rotate(-90 12 " + (TOP + BOT) / 2 + ")", "text-anchor": "middle", "font-size": 11, fill: "var(--muted)" }, f.label + " (" + f.unit + ")"));
      s.append(svg("rect", { x: X(tEnd), y: TOP, width: Math.max(0, R - X(tEnd)), height: BOT - TOP, fill: "var(--grid)", opacity: 0.45 }));
      const stepY = niceMax(ymax / 4);
      for (let v = 0; v <= ymax + 1e-9; v += stepY) {
        s.append(svg("line", { x1: L, x2: R, y1: Y(v), y2: Y(v), stroke: "var(--grid)" }),
          svg("text", { x: L - 8, y: Y(v) + 4, "text-anchor": "end", "font-size": 11, fill: "var(--muted)" }, fmt(v, stepY >= 1 ? 0 : 1)));
      }
      const colour = ["var(--good)", "var(--warning)", "var(--serious)", "var(--critical)"];
      d.bands.filter((b) => b.value <= ymax * 1.02).forEach((b) => {
        s.append(svg("line", { x1: L, x2: R, y1: Y(b.value), y2: Y(b.value), stroke: colour[b.level], "stroke-width": 1 }),
          svg("text", { x: R + 8, y: Y(b.value) + 4, "font-size": 11, fill: "var(--text2)" }, b.value + "+" + (narrow ? "" : " " + b.label)));
      });
      if (d.fan_limits) {
        [["start", "fan start"], ["stop", "fan stop"]].forEach(([k, name]) => {
          const v = d.fan_limits[k];
          if (v <= ymax * 1.02) s.append(svg("line", { x1: L, x2: R, y1: Y(v), y2: Y(v), stroke: "var(--text2)", "stroke-dasharray": "2 4" }),
            svg("text", { x: L + 4, y: Y(v) - 3, "font-size": 10.5, fill: "var(--text2)" }, name + " " + v));
        });
      }
      s.append(svg("line", { x1: X(tEnd), x2: X(tEnd), y1: TOP, y2: BOT, stroke: "var(--axis)" }),
        svg("text", { x: X(tEnd) - 5, y: TOP + 11, "text-anchor": "end", "font-size": 11, fill: "var(--text2)" }, d.live ? "now" : "end"));
      const line = (seg, key, Yf) => seg.map((r, i) => (i ? "L" : "M") + X(r.x).toFixed(1) + " " + (Yf || Y)(r[key]).toFixed(1)).join("");
      segments(rows, "measured", stepMs).forEach((seg) => {
        const p = line(seg, "measured");
        s.append(svg("path", { d: p + "L" + X(seg[seg.length - 1].x).toFixed(1) + " " + BOT + "L" + X(seg[0].x).toFixed(1) + " " + BOT + "Z", fill: "var(--s1)", opacity: 0.1 }),
          svg("path", { d: p, fill: "none", stroke: "var(--s1)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      });
      segments(rows, "forecast", stepMs).forEach((seg) => s.append(svg("path", { d: line(seg, "forecast"), fill: "none", stroke: "var(--s2)", "stroke-width": 2, "stroke-dasharray": "6 4", "stroke-linejoin": "round" })));
      const lastM = rows.filter((r) => r.measured !== undefined).pop(), lastF = rows.filter((r) => r.forecast !== undefined).pop();
      if (lastM) s.append(svg("circle", { cx: X(lastM.x), cy: Y(lastM.measured), r: 4, fill: "var(--s1)", stroke: "var(--surface)", "stroke-width": 2 }),
        svg("text", { x: X(lastM.x), y: Y(lastM.measured) - 9, "text-anchor": "end", "font-size": 12, "font-weight": 650, fill: "var(--text)" }, fmt(lastM.measured, f.digits)));
      if (lastF && (!lastM || lastF.x > lastM.x)) s.append(svg("circle", { cx: X(lastF.x), cy: Y(lastF.forecast), r: 4, fill: "var(--s2)", stroke: "var(--surface)", "stroke-width": 2 }),
        svg("text", { x: X(lastF.x) - 8, y: Y(lastF.forecast) + (lastM && lastF.forecast < lastM.measured ? 16 : -9), "text-anchor": "end", "font-size": 12, "font-weight": 650, fill: "var(--text)" }, fmt(lastF.forecast, f.digits)));

      // lab door
      const doorRows = rows.filter((r) => r.n_in !== undefined);
      const bw = Math.max(1.5, (R - L - 12) / ((t1 - t0) / stepMs) - 0.6);
      s.append(svg("rect", { x: L, y: 284, width: R - L, height: 32, fill: "var(--grid)", opacity: 0.5, rx: 2 }),
        svg("text", { x: L - 8, y: 297, "text-anchor": "end", "font-size": 11, fill: "var(--muted)" }, "Door in"),
        svg("text", { x: L - 8, y: 311, "text-anchor": "end", "font-size": 11, fill: "var(--muted)" }, "out"),
        svg("line", { x1: L, x2: R, y1: 300, y2: 300, stroke: "var(--axis)" }));
      const dmax = Math.max(1, ...doorRows.map((r) => Math.max(r.n_in, r.n_out)));
      doorRows.forEach((r) => {
        if (r.n_in) s.append(svg("rect", { x: X(r.x) - bw / 2, y: 300 - 14 * r.n_in / dmax, width: bw, height: 14 * r.n_in / dmax, fill: "var(--s3)" }));
        if (r.n_out) s.append(svg("rect", { x: X(r.x) - bw / 2, y: 300, width: bw, height: 14 * r.n_out / dmax, fill: "var(--ctx)" }));
      });
      const tin = doorRows.reduce((a, r) => a + r.n_in, 0), tout = doorRows.reduce((a, r) => a + r.n_out, 0);
      s.append(svg("text", { x: R + 8, y: 304, "font-size": 11, fill: "var(--text2)" }, doorRows.length ? (narrow ? tin + " / " + tout : "in " + tin + " · out " + tout) : (narrow ? "" : "no door count")));

      // strips: missing minutes stay gaps
      const strip = (keys, label, base, digits, fixedMax, unitText) => {
        const top = base - 40, have = rows.filter((r) => keys.some((k) => r[k] !== undefined && r[k] !== null));
        const max = fixedMax || Math.max(0, ...have.flatMap((r) => keys.map((k) => r[k] || 0)));
        const Ys = (v) => base - (max > 0 ? v / max : 0) * 36;
        s.append(svg("line", { x1: L, x2: R, y1: base, y2: base, stroke: "var(--axis)" }),
          svg("text", { x: L - 8, y: top + 10, "text-anchor": "end", "font-size": 11, fill: "var(--muted)" }, label),
          svg("text", { x: L - 8, y: top + 23, "text-anchor": "end", "font-size": 10, fill: "var(--muted)" }, max > 0 ? "top " + (digits === "sci" ? sci(max) : fmt(max, digits)) + (unitText || "") : ""));
        if (!have.length) { s.append(svg("text", { x: (L + R) / 2, y: base - 16, "text-anchor": "middle", "font-size": 12, fill: "var(--muted)" }, "unavailable in this range")); return; }
        keys.forEach((key, i) => segments(rows, key, stepMs).forEach((seg) => {
          const p = seg.map((r, j) => (j ? "L" : "M") + X(r.x).toFixed(1) + " " + Ys(r[key]).toFixed(1)).join("");
          if (i === 0) s.append(svg("path", { d: p + "L" + X(seg[seg.length - 1].x).toFixed(1) + " " + base + "L" + X(seg[0].x).toFixed(1) + " " + base + "Z", fill: "var(--ctx)", opacity: 0.22 }));
          s.append(svg("path", { d: p, fill: "none", stroke: i ? "var(--s3)" : "var(--ctx)", "stroke-width": 2, "stroke-linejoin": "round", "stroke-dasharray": i ? "4 3" : "" }));
        }));
      };
      strip(["persons_mean", "moving"], "Occupancy", 391, 0);
      s.append(svg("text", { x: R + 8, y: 375, "font-size": 10.5, fill: "var(--text2)" }, narrow ? "" : "present"),
        svg("text", { x: R + 8, y: 388, "font-size": 10.5, fill: "var(--s3)" }, narrow ? "" : "moving"));
      strip(["index"], "Motion index", 445, 0, 100, "");
      strip(["sigma2"], "Motion σ²", 499, "sci");
      strip(["speed"], "Speed", 553, 2, null, " BH/s");
      const spanMin = (t1 - t0) / 60000, tm = tickMinutes(spanMin, Math.max(2, Math.min(8, Math.floor((R - L) / (spanMin > 1500 ? 90 : 56))))) * 60000;
      for (let t = Math.ceil(t0 / tm) * tm; t <= t1; t += tm) {
        s.append(svg("line", { x1: X(t), x2: X(t), y1: 553, y2: 557, stroke: "var(--axis)" }),
          svg("text", { x: X(t), y: 570, "text-anchor": "middle", "font-size": 11, fill: "var(--muted)" }, spanMin > 1500 ? dayClock(t) : clock(t)));
      }
      const cross = svg("line", { x1: 0, x2: 0, y1: TOP, y2: 553, stroke: "var(--text2)", "stroke-width": 1, visibility: "hidden" });
      const hit = svg("rect", { x: L, y: 0, width: R - L, height: H, fill: "transparent" });
      s.append(cross, hit);
      const show = (evt) => {
        if (!rows.length) return;
        const box = s.getBoundingClientRect(), px = (evt.clientX - box.left) * (W / box.width);
        let best = rows[0];
        rows.forEach((r) => { if (Math.abs(X(r.x) - px) < Math.abs(X(best.x) - px)) best = r; });
        cross.setAttribute("x1", X(best.x)); cross.setAttribute("x2", X(best.x)); cross.setAttribute("visibility", "visible");
        tip.replaceChildren(el("div", "t", (spanMin > 1500 ? dayClock(best.x) : clock(best.x)) + (d.step > 1 ? " · " + d.step + "-minute average" : "") + (best.x > tEnd ? " · ahead" : "")));
        const u = " " + f.unit;
        tip.append(tipLine("var(--s1)", false, "Measured", best.measured !== undefined ? fmt(best.measured, f.digits) + u : "unavailable"));
        if (best.forecast !== undefined) tip.append(tipLine("var(--s2)", true, "Forecast", fmt(best.forecast, f.digits) + u));
        const v = (k, digits, unit) => (best[k] === undefined || best[k] === null ? "unavailable" : (digits === "sci" ? sci(best[k]) : fmt(best[k], digits)) + (unit || ""));
        tip.append(tipLine("var(--ctx)", false, "Persons (mean / max)", best.persons_mean === undefined ? "unavailable" : fmt(best.persons_mean, 1) + " / " + fmt(best.persons, 0)),
          tipLine("var(--s3)", true, "Moving", v("moving", 1)),
          tipLine("var(--ctx)", false, "Motion index", v("index", 0) + (best.motion !== undefined ? " (" + fmt(best.motion, 3) + " BH/s)" : "")),
          tipLine("var(--ctx)", false, "Temporal σ²", v("sigma2", "sci")),
          tipLine("var(--ctx)", false, "Walking speed", v("speed", 2, " BH/s")));
        if (best.n_in !== undefined) tip.append(tipLine("var(--s3)", false, "Door", "in " + best.n_in + " · out " + best.n_out));
        placeTip(tip, s, W, X(best.x));
      };
      hit.addEventListener("mousemove", show);
      hit.addEventListener("touchstart", (e) => show(e.touches[0]), { passive: true });
      hit.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; });
      tip.style.display = "none";
      host.replaceChildren(s, tip);
    }
    return {
      async load(field, scope) {
        try {
          data = await IAQ.getJSON("/api/chart?" + IAQ.rangeQuery() + "&scope=" + encodeURIComponent(scope || "inside") + (field ? "&field=" + field : ""));
        } catch (e) { return null; }
        render();
        return data;
      },
      render: render,
      get data() { return data; },
    };
  };

  // --------------------------------------------- small multiples, one time axis
  // panels: [{label, unit, digits ('sci' allowed), lines: [{key, name, colour, dash}], max}]
  IAQ.multiChart = function (host, d, panels) {
    const tip = el("div", "tip");
    if (!d || !d.rows) { host.replaceChildren(el("div", "empty", "Unavailable")); return; }
    const W = Math.max(320, host.clientWidth || 800), narrow = W < 600;
    const L = 76, R = W - (narrow ? 12 : 24), PH = 92, GAP = 22, TOP = 8;
    const H = TOP + panels.length * (PH + GAP) + 24;
    const t0 = ms(d.start), t1 = ms(d.end), stepMs = d.step * 60000;
    const rows = d.rows.filter((r) => ms(r.t) <= t1).map((r) => Object.assign({ x: ms(r.t) }, r));
    const X = (t) => L + 4 + (t - t0) / Math.max(1, t1 - t0) * (R - L - 8);
    const s = svg("svg", { viewBox: "0 0 " + W + " " + H, height: H, role: "img", "aria-label": panels.map((p) => p.label).join(", ") });
    const scales = panels.map((p, i) => {
      const top = TOP + i * (PH + GAP), base = top + PH;
      const vals = rows.flatMap((r) => p.lines.map((l) => r[l.key])).filter((v) => v !== undefined && v !== null);
      const max = p.max || niceMax(Math.max(1e-12, ...vals) * 1.05);
      const Y = (v) => base - (v / max) * (PH - 6);
      s.append(svg("rect", { x: L, y: top, width: R - L, height: PH, fill: "none", stroke: "var(--grid)" }),
        svg("text", { x: L + 6, y: top + 13, "font-size": 12, "font-weight": 600, fill: "var(--text)" }, p.label + (p.unit ? " (" + p.unit + ")" : "")),
        svg("text", { x: L - 6, y: top + 10, "text-anchor": "end", "font-size": 10.5, fill: "var(--muted)" }, p.digits === "sci" ? sci(max) : fmt(max, p.digits)),
        svg("text", { x: L - 6, y: base, "text-anchor": "end", "font-size": 10.5, fill: "var(--muted)" }, "0"));
      if (!vals.length) s.append(svg("text", { x: (L + R) / 2, y: top + PH / 2 + 4, "text-anchor": "middle", "font-size": 12, fill: "var(--muted)" }, "unavailable in this range"));
      p.lines.forEach((l) => segments(rows, l.key, stepMs).forEach((seg) => {
        const path = seg.map((r, j) => (j ? "L" : "M") + X(r.x).toFixed(1) + " " + Y(r[l.key]).toFixed(1)).join("");
        if (seg.length === 1) s.append(svg("circle", { cx: X(seg[0].x), cy: Y(seg[0][l.key]), r: 2, fill: l.colour }));
        s.append(svg("path", { d: path, fill: "none", stroke: l.colour, "stroke-width": 2, "stroke-linejoin": "round", "stroke-dasharray": l.dash ? "5 3" : "" }));
      }));
      return { top, base, Y };
    });
    const spanMin = (t1 - t0) / 60000, tm = tickMinutes(spanMin, Math.max(2, Math.min(8, Math.floor((R - L) / 70)))) * 60000;
    for (let t = Math.ceil(t0 / tm) * tm; t <= t1; t += tm) {
      s.append(svg("text", { x: X(t), y: H - 8, "text-anchor": "middle", "font-size": 11, fill: "var(--muted)" }, spanMin > 1500 ? dayClock(t) : clock(t)));
    }
    const cross = svg("line", { x1: 0, x2: 0, y1: TOP, y2: H - 22, stroke: "var(--text2)", visibility: "hidden" });
    const hit = svg("rect", { x: L, y: 0, width: R - L, height: H, fill: "transparent" });
    s.append(cross, hit);
    const show = (evt) => {
      if (!rows.length) return;
      const box = s.getBoundingClientRect(), px = (evt.clientX - box.left) * (W / box.width);
      let best = rows[0];
      rows.forEach((r) => { if (Math.abs(X(r.x) - px) < Math.abs(X(best.x) - px)) best = r; });
      cross.setAttribute("x1", X(best.x)); cross.setAttribute("x2", X(best.x)); cross.setAttribute("visibility", "visible");
      tip.replaceChildren(el("div", "t", (spanMin > 1500 ? dayClock(best.x) : clock(best.x)) + (d.step > 1 ? " · " + d.step + "-min average" : "")));
      panels.forEach((p) => p.lines.forEach((l) => {
        const v = best[l.key];
        tip.append(tipLine(l.colour, l.dash, l.name, v === undefined || v === null ? "unavailable" : (p.digits === "sci" ? sci(v) : fmt(v, p.digits)) + (p.unit ? " " + p.unit : "")));
      }));
      placeTip(tip, s, W, X(best.x));
    };
    hit.addEventListener("mousemove", show);
    hit.addEventListener("touchstart", (e) => show(e.touches[0]), { passive: true });
    hit.addEventListener("mouseleave", () => { cross.setAttribute("visibility", "hidden"); tip.style.display = "none"; });
    tip.style.display = "none";
    const legend = el("div", "legend");
    panels.forEach((p) => p.lines.forEach((l) => legend.append(legendKey(svg("line", { x1: 1, x2: 21, y1: 5, y2: 5, stroke: l.colour, "stroke-width": 2, "stroke-dasharray": l.dash ? "5 3" : "" }), l.name))));
    const wrap = el("div", "chart");
    wrap.append(s, tip);
    host.replaceChildren(legend, wrap);
  };

  // the standard motion / occupancy panels
  IAQ.MOTION_PANELS = [
    { label: "People", unit: "", digits: 1, lines: [{ key: "persons_mean", name: "Present", colour: "var(--ctx)" }, { key: "persons", name: "Max", colour: "var(--ctx)", dash: true }, { key: "ids", name: "IDs", colour: "var(--s1)", dash: true }] },
    { label: "Moving", unit: "", digits: 1, lines: [{ key: "moving", name: "Moving", colour: "var(--s3)" }] },
    { label: "Total motion", unit: "BH/s", digits: 3, lines: [{ key: "motion", name: "Total motion", colour: "var(--s2)" }] },
    { label: "Motion index", unit: "", digits: 0, max: 100, lines: [{ key: "index", name: "Index", colour: "var(--s2)", dash: true }] },
    { label: "σ² over time", unit: "", digits: "sci", lines: [{ key: "sigma2", name: "σ² time", colour: "var(--s1)" }] },
    { label: "Activity per person", unit: "BH/s", digits: 3, lines: [{ key: "mu_person", name: "Per person", colour: "var(--ctx)" }] },
    { label: "σ² between people", unit: "", digits: "sci", lines: [{ key: "sigma2_person", name: "σ² people", colour: "var(--s1)", dash: true }] },
    { label: "Walking speed", unit: "BH/s", digits: 2, lines: [{ key: "speed", name: "Speed", colour: "var(--s3)", dash: true }] },
  ];
})();
