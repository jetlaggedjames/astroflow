// Solve: plate-solve an image (offline ASTAP / built-in / optional online), then label every object in it.
import { api } from "../api.js";
import { chooseFiles, hint, previewUrl, runJob, statTiles } from "../common.js";
import { go, state } from "../state.js";
import { basename, clear, fmtNum, h, term, toast } from "../ui.js";
import { svgEl, svgText, Viewer } from "../viewer.js";

let el, ui = {}, path = null, ann = null, show = { dso: true, stars: true, labels: true, compass: true, scale: true };

export default {
  id: "solve", title: "Solve",
  create(root) { el = root; build(); },
  show(data) {
    if (data?.path) setPath(data.path);
    else if (!path) {
      const p = state.project;
      const cand = p?.process?.output || p?.stack?.output;
      if (cand) setPath(cand);
    }
    loadStatus();
  },
  key(e) { if (e.key === "f" || e.key === "F") ui.viewer.fitView(); if (e.key === "0") ui.viewer.actualSize(); },
};

function build() {
  ui.side = h("div", { class: "side" });
  ui.viewer = new Viewer();
  ui.viewer.overlays.push(drawAnnotations);
  ui.toolbar = h("div", { class: "toolbar" });
  el.append(h("div", { class: "split" }, ui.side, h("div", { class: "center" }, ui.toolbar, ui.viewer.el)));
  ui.file = h("div", { class: "panel" });
  ui.status = h("div");
  ui.form = h("div", { class: "panel" });
  ui.job = h("div");
  ui.result = h("div");
  ui.side.append(hint(term("Plate solving"), " works out exactly where in the sky a picture was taken by matching its stars against a star catalogue. Then every galaxy, nebula and named star in the picture can be labelled."),
    ui.file, ui.status, ui.form, ui.job, ui.result);
  for (const [k, l] of [["dso", "Deep-sky objects"], ["stars", "Named stars"], ["labels", "Labels"], ["compass", "Compass"], ["scale", "Scale bar"]])
    ui.toolbar.append(h("label", { class: "check tiny" }, h("input", { type: "checkbox", checked: show[k], onchange: (e) => { show[k] = e.target.checked; ui.viewer.draw(); } }), l));
  ui.toolbar.append(h("span", { class: "spacer" }), h("button", { class: "small", onclick: () => ui.viewer.fitView() }, "Fit"), h("button", { class: "small", onclick: () => ui.viewer.actualSize() }, "100%"));
  renderFile(); renderForm();
}

async function setPath(p) {
  path = p; ann = null; renderFile(); clear(ui.result);
  try { await ui.viewer.setImage(previewUrl(p, 2000), { keepView: false }); } catch { /* */ }
  loadAnnotations();
}

function renderFile() {
  const box = ui.file; clear(box);
  box.append(h("h3", {}, "Image"), path ? h("div", { class: "mono tiny", style: { wordBreak: "break-all" } }, basename(path)) : h("div", { class: "tiny" }, "Choose a stacked or single image (videos: the middle frame is used)."),
    h("div", { class: "row", style: { marginTop: "6px" } },
      h("button", { class: "small", onclick: async () => { const f = await chooseFiles({ multiple: false, title: "Image to plate solve" }); if (f) setPath(f[0]); } }, "Choose image..."),
      state.selection.length === 1 ? h("button", { class: "small", onclick: () => setPath(state.selection[0]) }, "Library selection") : ""));
}

async function loadStatus() {
  const box = ui.status; clear(box);
  let s;
  try { s = await api.get("/api/solve/status", { quiet: true }); } catch { return; }
  const fov = s.fov_deg || [0, 0];
  const offline = s.astap && s.databases?.length;
  box.append(h("div", { class: offline ? "okbox tiny" : "warnbox tiny" },
    offline ? `Offline solver ready: ASTAP with ${s.databases.map((d) => d.toUpperCase()).join(", ")} star database.` :
      s.astap ? `ASTAP is installed but no star database was found. Recommended for your ${fmtNum(fov[0], 2)}° x ${fmtNum(fov[1], 2)}° field: ${String(s.recommended_db?.name || "none").toUpperCase()}.`
        : `The offline solver (ASTAP) is not installed yet. Settings > Plate solving downloads it (recommended database for your field: ${String(s.recommended_db?.name || "none").toUpperCase()}).`,
    !offline && !s.builtin_ok ? h("div", {}, s.builtin_note) : "",
    s.online_allowed ? h("div", {}, `Online fallback (Astrometry.net) is allowed${s.online_key ? "" : " but no API key is set"}.`) : "",
    !offline ? h("button", { class: "small", style: { marginTop: "4px" }, onclick: () => go("settings", { section: "solver" }) }, "Set up plate solving") : ""));
}

function renderForm() {
  const box = ui.form; clear(box);
  const target = h("input", { placeholder: "optional: M27, NGC 7000, or leave empty", style: { width: "100%" } });
  const ra = h("input", { placeholder: "RA e.g. 19h59m36s or 299.9", style: { width: "100%" } });
  const dec = h("input", { placeholder: "Dec e.g. +22°43' or 22.72", style: { width: "100%" } });
  const engine = h("select", {}, h("option", { value: "" }, "Automatic (best available)"), h("option", { value: "astap" }, "ASTAP (offline)"), h("option", { value: "builtin" }, "Built-in (offline, wide fields)"), h("option", { value: "online" }, "Astrometry.net (online)"));
  box.append(h("h3", {}, "Solve"),
    h("div", { class: "field" }, h("label", { "data-tip": "Speeds things up a lot. AstroFlow also reads the target and mount position saved in the file, if any." }, "Roughly what is it? (hint)"), target),
    h("div", { class: "grid2 adv" }, h("div", { class: "field" }, h("label", {}, "RA hint"), ra), h("div", { class: "field" }, h("label", {}, "Dec hint"), dec)),
    h("div", { class: "field adv" }, h("label", {}, "Solver"), engine),
    h("button", { class: "primary big", style: { width: "100%" }, onclick: async () => {
      if (!path) { toast("Choose an image first", "warn"); return; }
      const body = { path, engine: engine.value || undefined, project_id: state.project?.id };
      if (target.value.trim()) body.hint = target.value.trim();
      else if (ra.value && dec.value) { body.ra = parseCoord(ra.value, true); body.dec = parseCoord(dec.value, false); }
      try {
        const { job } = await api.post("/api/solve", body);
        const r = await runJob(job, ui.job, { title: "Plate solving" });
        if (!r.solved) { clear(ui.result); ui.result.append(h("div", { class: "warnbox" }, h("b", {}, "Not solved. "), ...(r.notes || []).map((n) => h("div", { class: "tiny" }, n)), h("div", { class: "tiny" }, "Tips: give a target hint, make sure stars are visible (stacked deep-sky images work best), check focal length and pixel size in Settings."))); return; }
        toast("Solved!", "ok");
        loadAnnotations(r.notes);
      } catch { /* shown */ }
    } }, "Plate solve"));
}

function parseCoord(s, isRa) {
  const t = s.trim();
  if (/^[-+]?\d+(\.\d+)?$/.test(t)) return parseFloat(t);
  const nums = t.split(/[^\d.+-]+/).filter(Boolean).map(Number);
  const sign = t.trim().startsWith("-") ? -1 : 1;
  const v = Math.abs(nums[0] || 0) + (nums[1] || 0) / 60 + (nums[2] || 0) / 3600;
  return isRa && /h/i.test(t) ? v * 15 : sign * v;
}

async function loadAnnotations(notes) {
  if (!path) return;
  try { ann = await api.get("/api/solve/annotations?" + api.q({ path, stars: true }), { quiet: true }); } catch { ann = null; }
  ui.viewer.draw();
  const box = ui.result; clear(box);
  if (!ann || !ann.solved) { box.append(h("div", { class: "tiny" }, "This image has not been solved yet.")); return; }
  const s = ann.solution;
  const objs = ann.objects || [];
  box.append(h("div", { class: "panel" }, h("h3", {}, "Solution"),
    statTiles([[fmtRa(s.ra), "RA (centre)"], [fmtDec(s.dec), "Dec (centre)"], [`${fmtNum(s.scale_arcsec, 3)}"`, "per pixel"],
      [`${fmtNum(s.rotation_deg, 1)}°`, "north angle"], [`${fmtNum(s.fov_deg[0] * 60, 1)}' x ${fmtNum(s.fov_deg[1] * 60, 1)}'`, "field of view"]]),
    h("div", { class: "tiny" }, `Solved by ${s.solver}${s.seconds >= 0.05 ? ` in ${fmtNum(s.seconds, 1)} s` : ""}${s.stars_matched ? `, ${s.stars_matched} stars matched` : ""}${s.flipped ? ", image is mirrored" : ""}.`),
    ...(notes || []).map((n) => h("div", { class: "tiny" }, n)),
    h("h4", { style: { marginTop: "8px" } }, `${objs.length} object(s) in view`),
    h("div", { class: "listbox", style: { maxHeight: "260px" } }, ...objs.map((o) => h("div", { class: "item tiny", onclick: () => focusObj(o) },
      h("b", {}, o.label), h("span", { style: { flex: 1 } }, o.kind || ""), o.mag !== null && o.mag !== undefined ? h("span", {}, `mag ${o.mag}`) : ""))),
    h("div", { class: "row", style: { marginTop: "8px" } },
      h("button", { onclick: () => go("export", { paths: [path], annotate: true }) }, "Export with labels →"),
      h("button", { onclick: () => go("process", { path }) }, "Post-process →"))));
}

function focusObj(o) {
  const v = ui.viewer; if (!v.img) return;
  const f = v.img.naturalWidth / (ann.solution.width || v.img.naturalWidth);
  const r = v.el.getBoundingClientRect();
  v.zoom = Math.max(v.zoom, 1); v.ox = r.width / 2 - o.x * f * v.zoom; v.oy = r.height / 2 - o.y * f * v.zoom; v.fit = false; v.draw();
}

function drawAnnotations(svg, v) {
  if (!ann || !ann.solved || !v.img) return;
  const f = v.img.naturalWidth / (ann.solution.width || v.img.naturalWidth);
  const col = "#7fd4ff";
  for (const o of ann.objects || []) {
    const star = o.catalog === "stars";
    if (star ? !show.stars : !show.dso) continue;
    const [x, y] = v.toScreen(o.x * f, o.y * f);
    const rx = Math.max(6, (o.rx || 0) * f * v.zoom), ry = Math.max(6, (o.ry || o.rx || 0) * f * v.zoom);
    if (star) svg.append(svgEl("circle", { cx: x, cy: y, r: 9, fill: "none", stroke: "#ffe08a", "stroke-width": 1.2 }));
    else svg.append(svgEl("ellipse", { cx: x, cy: y, rx, ry, fill: "none", stroke: col, "stroke-width": 1.5, transform: `rotate(${o.angle || 0} ${x} ${y})` }));
    if (show.labels) svg.append(svgText(x + (star ? 11 : rx * 0.75 + 4), y - (star ? 6 : ry * 0.75), o.label, { "font-size": star ? 11 : 13, fill: star ? "#ffe08a" : col }));
  }
  const r = v.el.getBoundingClientRect();
  if (show.compass && ann.compass) {
    const cx = r.width - 70, cy = r.height - 80, L = 40;
    for (const [k, vec, c] of [["N", ann.compass.north, "#ff7070"], ["E", ann.compass.east, "#fff"]]) {
      svg.append(svgEl("line", { x1: cx, y1: cy, x2: cx + vec[0] * L, y2: cy + vec[1] * L, stroke: c, "stroke-width": 2 }));
      svg.append(svgText(cx + vec[0] * (L + 10) - 4, cy + vec[1] * (L + 10) + 4, k, { "font-size": 12 }));
    }
  }
  if (show.scale && ann.scale_bar) {
    const len = ann.scale_bar.pixels * f * v.zoom;
    const x0 = 20, y0 = r.height - 30;
    svg.append(svgEl("line", { x1: x0, y1: y0, x2: x0 + len, y2: y0, stroke: "#fff", "stroke-width": 3 }), svgText(x0, y0 - 8, ann.scale_bar.label, { "font-size": 12 }));
  }
}

function fmtRa(deg) { const hrs = (((deg / 15) % 24) + 24) % 24; const hh = Math.floor(hrs), mm = Math.floor((hrs - hh) * 60), ss = ((hrs - hh) * 60 - mm) * 60; return `${hh}h${String(mm).padStart(2, "0")}m${ss.toFixed(0).padStart(2, "0")}s`; }
function fmtDec(deg) { const s = deg < 0 ? "-" : "+"; const a = Math.abs(deg); const d = Math.floor(a), m = Math.floor((a - d) * 60); return `${s}${d}°${String(m).padStart(2, "0")}'`; }
