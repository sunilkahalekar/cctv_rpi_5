/* Camera picture with boxes, IDs, confidence and detection areas drawn by the browser,
   and the detection-area editor. The picture and its boxes always belong to the same
   analysed frame (fetched by sequence number). */
(function () {
  "use strict";
  const { el, svg, fmt } = IAQ;

  IAQ.viewOpts = { boxes: true, ids: true, roi: true, outside: true };
  try { Object.assign(IAQ.viewOpts, JSON.parse(localStorage.getItem("viewOpts") || "{}")); } catch (e) { /* ignore */ }
  IAQ.saveViewOpts = () => { try { localStorage.setItem("viewOpts", JSON.stringify(IAQ.viewOpts)); } catch (e) { /* ignore */ } };

  // detection areas per camera, cached
  const roiCache = {};
  IAQ.roi = async function (cam, fresh) {
    const c = roiCache[cam];
    if (!fresh && c && Date.now() - c.at < 60000) return c.data;
    try {
      const data = await IAQ.getJSON("/api/roi/" + cam);
      roiCache[cam] = { at: Date.now(), data: data };
      return data;
    } catch (e) { return c ? c.data : { polygons: [], operator: false }; }
  };
  IAQ.forgetRoi = (cam) => { delete roiCache[cam]; };

  const COL = { held: "#3fa9ff", still: "#5ee65e", moving: "#ff8c1a", outside: "#a0a0a0", area: "#ffd400", areaOff: "#9a9a9a", hi: "#3fa9ff" };

  IAQ.CamView = function (host, opts) {
    opts = opts || {};
    host.classList.add("camview");
    const img = el("img"), over = svg("svg", { viewBox: "0 0 1000 1000", preserveAspectRatio: "none", "aria-hidden": "true" });
    const labels = el("div", "labels"), msg = el("div", "camsg");
    img.alt = "camera picture";
    host.replaceChildren(img, over, labels, msg);
    let cam = null, shownSeq = null, people = [], polygons = [], highlight = null, crop = null;

    function drawOverlay() {
      over.replaceChildren(); labels.replaceChildren();
      const o = Object.assign({}, IAQ.viewOpts, opts.force || {});
      if ((o.roi || opts.keepCrop) && crop && !opts.small) {
        // the parts of the picture YOLO looks at (one per area: small far areas are enlarged)
        (Array.isArray(crop[0]) ? crop : [crop]).forEach((r) => {
          over.append(svg("rect", { x: r[0] * 1000, y: r[1] * 1000, width: (r[2] - r[0]) * 1000, height: (r[3] - r[1]) * 1000,
            fill: "none", stroke: "#fff", "stroke-opacity": 0.7, "stroke-width": 1.5, "stroke-dasharray": "3 5", "vector-effect": "non-scaling-stroke" }));
          const t = el("div", "croptag", "YOLO");
          t.title = "YOLO looks only inside this rectangle (a detection area plus a margin), enlarged to the model size";
          t.style.left = r[0] * 100 + "%"; t.style.top = r[1] * 100 + "%";
          labels.append(t);
        });
      }
      if (o.roi) polygons.forEach((p, i) => {
        const pts = p.points.map(([x, y]) => x * 1000 + "," + y * 1000).join(" ");
        const colour = highlight === i ? COL.hi : p.enabled ? COL.area : COL.areaOff;
        over.append(svg("polygon", { points: pts, fill: colour, "fill-opacity": highlight === i ? 0.18 : 0.06, stroke: "#000", "stroke-width": 4, "vector-effect": "non-scaling-stroke" }),
          svg("polygon", { points: pts, fill: "none", stroke: colour, "stroke-width": 2, "stroke-dasharray": p.enabled ? "" : "6 4", "vector-effect": "non-scaling-stroke" }));
        if (!opts.small) {
          const tag = el("div", "areatag", p.name + (p.enabled ? "" : " (off)"));
          tag.style.left = p.points[0][0] * 100 + "%"; tag.style.top = p.points[0][1] * 100 + "%";
          labels.append(tag);
        }
      });
      if (!o.boxes) return;
      people.forEach((p) => {
        if (!p.in_roi && !o.outside) return;
        const [x1, y1, x2, y2] = p.box;
        const colour = p.held ? COL.held : p.new ? COL.outside : !p.in_roi ? COL.outside : p.moving ? COL.moving : COL.still;
        const dash = p.held ? "8 4" : p.new ? "2 3" : p.in_roi ? "" : "4 3";
        over.append(svg("rect", { x: x1 * 1000, y: y1 * 1000, width: (x2 - x1) * 1000, height: (y2 - y1) * 1000, fill: "none", stroke: colour, "stroke-width": p.in_roi ? 2 : 1, "stroke-dasharray": dash, "vector-effect": "non-scaling-stroke" }),
          svg("circle", { cx: (x1 + x2) * 500, cy: y2 * 1000, r: 4, fill: colour, "vector-effect": "non-scaling-stroke" }));
        if (o.ids && !opts.small) {
          const t = el("div", "boxtag" + (p.in_roi ? "" : " out"), "#" + p.id + " " + (p.held ? "⏸ " + Math.round(p.since) + "s" : p.new ? "new" : fmt(p.conf, 2)));
          t.style.left = x1 * 100 + "%"; t.style.top = y1 * 100 + "%"; t.style.background = colour;
          if (p.held) { t.title = "person " + p.id + " not found in this picture but still counted (was sitting here " + Math.round(p.since) + " s ago)"; labels.append(t); return; }
          t.title = "person " + p.id + " · confidence " + fmt(p.conf, 2) + (p.new ? " · new: counted once seen again" : p.in_roi ? "" : " · outside the detection area (not counted)")
            + " · activity " + fmt(p.activity, 3) + " BH/s · speed " + (p.speed === null ? "first picture" : fmt(p.speed, 2) + " BH/s");
          labels.append(t);
        }
      });
    }

    return {
      host: host,
      async set(c, force) {
        cam = c;
        if (!c) return;
        if (c.frame_size) host.style.aspectRatio = c.frame_size[0] + " / " + c.frame_size[1];
        host.dataset.setup = c.setup || "";
        const roi = await IAQ.roi(c.id);
        polygons = (roi.mode || "area") === "area" ? roi.polygons || [] : [];
        if (!opts.keepCrop) crop = c.crop || null;
        if (!IAQ.showVideo) { msg.textContent = "Pictures are switched off (dashboard: show_video)"; msg.hidden = false; return; }
        if (!c.online) {
          msg.textContent = "Offline" + (c.last_seen ? " · last picture " + c.last_seen : " · no picture yet");
          msg.hidden = false; host.classList.add("offline");
        } else { msg.hidden = true; host.classList.remove("offline"); }
        if (!c.frame_seq || (c.frame_seq === shownSeq && !force)) { drawOverlay(); return; }
        const seq = c.frame_seq, ppl = c.people || [];
        const probe = new Image();
        probe.onload = () => {
          if (cam && cam.id === c.id) { img.src = probe.src; shownSeq = seq; people = ppl; drawOverlay(); }
        };
        probe.src = "/frame/" + c.id + ".jpg?seq=" + seq;
      },
      redraw: drawOverlay,
      highlight(i) { highlight = i; drawOverlay(); },
      setPolygons(p) { polygons = p; drawOverlay(); },
      setCrop(r) { crop = r; drawOverlay(); },
      get cam() { return cam; },
    };
  };

  // ------------------------------------------------------- detection-area editor
  IAQ.RoiEditor = function (host, onChange) {
    const view = IAQ.CamView(host, { force: { boxes: true, ids: false, roi: false, outside: true }, keepCrop: true });
    const edit = svg("svg", { viewBox: "0 0 1000 1000", preserveAspectRatio: "none", class: "editlayer" });
    const handles = el("div", "handles");
    host.append(edit, handles);
    let polys = [], sel = null, drawing = null, drag = null, readOnly = true;

    function norm(evt) {
      const b = host.getBoundingClientRect();
      return [Math.min(1, Math.max(0, (evt.clientX - b.left) / b.width)), Math.min(1, Math.max(0, (evt.clientY - b.top) / b.height))]
        .map((v) => Math.round(v * 10000) / 10000);
    }
    function draw(keepHandles) {
      edit.replaceChildren();
      if (!keepHandles) handles.replaceChildren();
      const all = polys.map((p, i) => ({ p: p, i: i })).concat(drawing ? [{ p: { points: drawing, enabled: true, name: "new" }, i: -1 }] : []);
      all.forEach(({ p, i }) => {
        const pts = p.points.map(([x, y]) => x * 1000 + "," + y * 1000).join(" ");
        const active = i === sel || i === -1;
        const colour = active ? "#3fa9ff" : p.enabled ? "#ffd400" : "#9a9a9a";
        const shape = i === -1 ? "polyline" : "polygon";
        edit.append(svg(shape, { points: pts, fill: active && i !== -1 ? "#3fa9ff" : "none", "fill-opacity": 0.15, stroke: "#000", "stroke-width": 5, "vector-effect": "non-scaling-stroke" }),
          svg(shape, { points: pts, fill: "none", stroke: colour, "stroke-width": 2.5, "stroke-dasharray": p.enabled ? "" : "6 4", "vector-effect": "non-scaling-stroke" }));
        if (active && !keepHandles) p.points.forEach(([x, y], k) => {
          const h = el("div", "handle");
          h.style.left = x * 100 + "%"; h.style.top = y * 100 + "%";
          h.dataset.k = k;
          h.title = "drag to move this corner";
          handles.append(h);
        });
      });
    }
    handles.addEventListener("pointerdown", (e) => {
      if (readOnly || drawing || sel === null || !e.target.classList.contains("handle")) return;
      drag = +e.target.dataset.k; e.target.setPointerCapture(e.pointerId); e.preventDefault(); e.stopPropagation();
    });
    handles.addEventListener("pointermove", (e) => {
      if (drag === null || sel === null) return;
      const p = norm(e);
      polys[sel].points[drag] = p;
      e.target.style.left = p[0] * 100 + "%"; e.target.style.top = p[1] * 100 + "%";
      draw(true);
    });
    handles.addEventListener("pointerup", () => { if (drag !== null) { drag = null; onChange("changed"); } });
    edit.addEventListener("pointerdown", (e) => {
      if (readOnly) return;
      if (drawing) { drawing.push(norm(e)); draw(); onChange("drawing", drawing.length); return; }
      // click inside an area selects it
      const [x, y] = norm(e);
      const hit = polys.findIndex((p) => pointIn(x, y, p.points));
      if (hit >= 0) { sel = hit; draw(); onChange("select", sel); }
    });
    function pointIn(x, y, pts) {
      let inside = false;
      for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
        const [xi, yi] = pts[i], [xj, yj] = pts[j];
        if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
      }
      return inside;
    }
    return {
      view: view,
      setReadOnly(v) { readOnly = v; host.classList.toggle("editing", !v); },
      load(p) { polys = JSON.parse(JSON.stringify(p || [])); sel = null; drawing = null; draw(); },
      get polygons() { return polys; },
      get selected() { return sel; },
      select(i) { sel = i; draw(); },
      startDrawing() { drawing = []; sel = null; draw(); },
      finish(name) {
        if (!drawing || drawing.length < 3) return false;
        polys.push({ name: name, enabled: true, points: drawing });
        sel = polys.length - 1; drawing = null; draw(); return true;
      },
      cancel() { drawing = null; draw(); },
      get drawing() { return drawing; },
      remove(i) { polys.splice(i, 1); sel = null; draw(); },
      redraw: draw,
    };
  };
})();
