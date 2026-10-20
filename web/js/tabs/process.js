// Post-process: a non-destructive list of editing steps (sharpen, stretch, colour...), masks, presets, undo history.
import { api } from "../api.js";
import { drawHistogram } from "../charts.js";
import { chooseFiles, hint, runJob } from "../common.js";
import { go, state } from "../state.js";
import { basename, clear, confirmDialog, debounce, fmtNum, h, modal, paramForm, promptDialog, section, toast } from "../ui.js";
import { Viewer } from "../viewer.js";

let el, ui = {}, ops = null, opById = {}, path = null, steps = [], masks = {}, selUid = null, target = null,
  renderSeq = 0, beforeKey = null, maskPreview = null, cropEdit = false, lastFull = null, notes = [];

export default {
  id: "process", title: "Post-process",
  async create(root) { el = root; build(); ops = await state.loadOps(); ops.ops.forEach((o) => { opById[o.id] = o; }); paintLibrary(); paintPresets(); paintSteps(); state.on("mode", () => { paintLibrary(); paintParams(); }); },
  async show(data) {
    if (!ops) { ops = await state.loadOps(); ops.ops.forEach((o) => { opById[o.id] = o; }); }
    const p = state.project;
    if (data?.path) {
      if (p && p.process?.base === data.path) loadFromProject(p);
      else { open(data.path, [], {}); }
    } else if (!path && p?.process?.base) loadFromProject(p);
    paintHistory();
  },
  hide() { ui.viewer.tool = "pan"; },
  key(e) {
    const ctrl = e.ctrlKey || e.metaKey;
    if (ctrl && (e.key === "z" || e.key === "Z") && !e.shiftKey) { e.preventDefault(); undo(); }
    else if (ctrl && (e.key === "y" || (e.shiftKey && (e.key === "z" || e.key === "Z")))) { e.preventDefault(); redo(); }
    else if (e.key === "b" || e.key === "B") toggleBefore();
    else if (e.key === "f" || e.key === "F") ui.viewer.fitView();
    else if (e.key === "0") ui.viewer.actualSize();
    else if (e.key === "Delete" && selUid) removeStep(selUid);
  },
};

function loadFromProject(p) {
  open(p.process.base, p.process.steps || [], p.process.masks || {});
}

function open(p, st, mk) {
  path = p; steps = JSON.parse(JSON.stringify(st || [])); masks = JSON.parse(JSON.stringify(mk || {})); selUid = steps.length ? steps[steps.length - 1].uid : null;
  beforeKey = null; ui.viewer.split = null; ui.viewer.before = null; maskPreview = null; cropEdit = false; target = null;
  ui.file.textContent = basename(p);
  ui.empty.style.display = "none";
  paintSteps(); paintParams(); paintMasks(); paintHistory();
  render(true);
}

// ------------------------------------------------------------------ layout
function build() {
  ui.lib = h("div", { class: "side", style: { width: "300px", minWidth: "250px" } });
  ui.viewer = new Viewer({ onPaint, onSelectRect });
  ui.toolbar = h("div", { class: "toolbar" });
  ui.empty = h("div", { class: "empty", style: { position: "absolute", inset: "60px 0 0 0", pointerEvents: "none" } }, h("h3", {}, "Nothing open"),
    h("p", {}, "Stack something first, or choose an image (TIFF, FITS, PNG...)."));
  ui.hist = h("canvas", { class: "chart", style: { height: "70px" } });
  ui.info = h("div", { class: "tiny", style: { padding: "2px 8px" } });
  const center = h("div", { class: "center" }, ui.toolbar, ui.viewer.el, ui.empty,
    h("div", { style: { padding: "4px 8px", background: "var(--bg2)", borderTop: "1px solid var(--border)" } }, ui.hist, ui.info));
  ui.right = h("div", { class: "side right wide" });
  el.append(h("div", { class: "split" }, ui.lib, center, ui.right));
  buildToolbar();
  ui.stepsBox = h("div", { class: "steplist" });
  ui.params = h("div");
  ui.masksBox = h("div");
  ui.histBox = h("div");
  ui.save = h("div");
  ui.right.append(
    hint("Start with ", h("b", {}, "Auto"), " (left) for a good first result, then adjust any step. Nothing is permanent: steps can be changed, switched off or removed at any time, and the original stays untouched."),
    h("div", { class: "panel" }, h("div", { class: "row" }, h("h3", { style: { margin: 0, flex: 1 } }, "Steps"),
      h("button", { class: "small", onclick: undo, title: "Undo (Ctrl+Z)" }, "↶ Undo"), h("button", { class: "small", onclick: redo, title: "Redo (Ctrl+Y)" }, "↷ Redo")),
      ui.stepsBox),
    ui.params,
    section("Masks (apply a step to part of the image)", ui.masksBox, { open: false }),
    section("History", ui.histBox, { open: false }),
    ui.save);
}

function buildToolbar() {
  ui.file = h("b", { class: "mono", style: { maxWidth: "170px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, "");
  ui.beforeBtn = h("button", { class: "small", onclick: toggleBefore, title: "Compare before / after (B)" }, "Before / after");
  ui.toolbar.append(
    h("button", { class: "small", onclick: async () => { const f = await chooseFiles({ multiple: false, title: "Open an image to edit" }); if (f) open(f[0], [], {}); } }, "Open..."),
    ui.file, h("span", { class: "spacer" }),
    ui.beforeBtn,
    h("button", { class: "small", onclick: () => ui.viewer.fitView(), title: "Fit (F)" }, "Fit"),
    h("button", { class: "small", onclick: () => ui.viewer.actualSize(), title: "100% (0)" }, "100%"),
    h("button", { class: "small", onclick: detailPreview, "data-tip": "Shows the area you are looking at, processed at full resolution (the normal preview is reduced for speed)." }, "Full-res detail"),
    h("span", { class: "sep" }),
    h("button", { class: "small primary", onclick: saveFull, "data-tip": "Process the full-size image and save a 32-bit FITS + 16-bit TIFF master in the project folder." }, "Save result"),
    h("button", { class: "small", onclick: () => go("export", { paths: [path], project: true }) }, "Export →"));
}

// ------------------------------------------------------------------ library
function paintLibrary() {
  const box = ui.lib; clear(box);
  if (!ops) return;
  box.append(h("div", { class: "panel" }, h("h3", {}, "Automatic"),
    h("div", { class: "tiny" }, "Builds a starting recipe for the kind of target (planet, Moon or deep sky). Only runs when you press it."),
    h("div", { class: "row", style: { marginTop: "6px" } },
      h("button", { class: "primary", onclick: () => auto() }, "Auto"),
      h("select", { id: "autoTarget" }, h("option", { value: "" }, "detect target"), h("option", { value: "planet" }, "Planet"), h("option", { value: "moon" }, "Moon"), h("option", { value: "dso" }, "Deep sky"))),
    target ? h("div", { class: "tiny" }, `Looks like: ${{ planet: "a planet", moon: "the Moon", dso: "deep sky" }[target] || target}`) : ""));
  box.append(ui.presetBox = h("div", { class: "panel" }));
  paintPresets();
  const t = target;
  if (t && !state.advanced) {
    const rec = { planet: ["wavelets", "deconvolution", "rgb_align", "denoise", "levels", "saturation", "limb_cleanup"], moon: ["wavelets", "local_contrast", "deconvolution", "denoise", "curves", "lunar_color"],
      dso: ["background_extraction", "color_calibration", "arcsinh_stretch", "auto_stretch", "denoise", "star_reduction", "saturation", "curves"] }[t] || [];
    box.append(h("div", { class: "panel" }, h("h3", {}, "Recommended tools"), h("div", { class: "oplib" }, ...rec.filter((id) => opById[id]).map((id) => opButton(opById[id])))));
  }
  for (const cat of ops.categories) {
    const list = ops.ops.filter((o) => o.category === cat);
    if (!list.length) continue;
    box.append(section(cat, h("div", { class: "oplib" }, ...list.map(opButton)), { open: state.advanced }));
  }
}
function opButton(o) {
  return h("button", { "data-tip": o.description + (o.linear_input ? " Works best early, before stretching." : ""), onclick: () => addStep(o.id) },
    o.name, o.gpu && state.status?.gpu?.available ? h("span", { class: "chip", style: { marginLeft: "auto" } }, "GPU") : "");
}

function paintPresets() {
  const box = ui.presetBox; if (!box || !ops) return; clear(box);
  const first = ops.presets.find((p) => p.target === target)?.name;
  const sel = h("select", { style: { flex: 1 } }, ...ops.presets.map((p) => h("option", { value: p.name, selected: p.name === first }, `${p.name}${p.builtin ? "" : " (yours)"}`)));
  box.append(h("h3", {}, "Presets"),
    h("div", { class: "row" }, sel, h("button", { class: "small", onclick: () => applyPreset(sel.value) }, "Apply")),
    h("div", { class: "row", style: { marginTop: "6px" } },
      h("button", { class: "small", onclick: savePreset }, "Save current as preset..."),
      h("button", { class: "small ghost", onclick: async () => {
        const p = ops.presets.find((x) => x.name === sel.value);
        if (!p || p.builtin) { toast("Built-in presets cannot be deleted", "warn"); return; }
        if (await confirmDialog("Delete preset", `Delete your preset "${p.name}"? Images already edited with it are not affected.`, { ok: "Delete", danger: true })) {
          await api.del("/api/process/presets?" + api.q({ name: p.name })); ops.presets = await api.get("/api/process/presets"); paintPresets();
        }
      } }, "Delete")));
}

async function applyPreset(name) {
  if (!path) { toast("Open an image first", "warn"); return; }
  if (steps.length && !(await confirmDialog("Apply preset", `Replace your ${steps.length} current step(s) with "${name}"? (You can undo this.)`, { ok: "Replace" }))) return;
  const p = await api.get("/api/process/preset?" + api.q({ name }));
  steps = p.steps; masks = p.masks || {}; selUid = steps[0]?.uid || null;
  changed(`Preset: ${name}`, true);
}
async function savePreset() {
  if (!steps.length) { toast("Add some steps first", "warn"); return; }
  const name = await promptDialog("Save preset", "Name (e.g. 'Jupiter good seeing')", "");
  if (!name) return;
  await api.post("/api/process/presets", { name, steps, masks, target: target || "" });
  ops.presets = await api.get("/api/process/presets"); paintPresets(); toast("Preset saved", "ok");
}

async function auto() {
  if (!path) { toast("Open an image first", "warn"); return; }
  const t = document.getElementById("autoTarget")?.value || undefined;
  if (steps.length && !(await confirmDialog("Auto", `Replace your ${steps.length} current step(s) with an automatic recipe? (You can undo this.)`, { ok: "Replace" }))) return;
  const r = await api.post("/api/process/auto", { path, target: t });
  steps = r.steps; target = r.target; selUid = steps[steps.length - 1]?.uid || null;
  changed(`Auto (${r.target})`, true);
  paintLibrary();
}

// -------------------------------------------------------------------- steps
function newUid() { return Math.random().toString(36).slice(2, 10); }
function addStep(opId) {
  if (!path) { toast("Open an image first", "warn"); return; }
  const o = opById[opId];
  const st = { uid: newUid(), op: opId, params: Object.fromEntries(o.params.map((p) => [p.name, p.default])), enabled: true, mask: null, opacity: 1 };
  const i = steps.findIndex((s) => s.uid === selUid);
  if (o.geometric && o.id === "crop") steps.unshift(st); else steps.splice(i >= 0 ? i + 1 : steps.length, 0, st);
  selUid = st.uid;
  changed(`Add ${o.name}`, true);
  if (o.id === "crop") startCrop();
}
function removeStep(uid) {
  const s = steps.find((x) => x.uid === uid); if (!s) return;
  steps = steps.filter((x) => x.uid !== uid);
  if (selUid === uid) selUid = steps[steps.length - 1]?.uid || null;
  changed(`Remove ${opById[s.op]?.name || s.op}`, true);
}
function moveStep(uid, d) {
  const i = steps.findIndex((s) => s.uid === uid); const j = i + d;
  if (i < 0 || j < 0 || j >= steps.length) return;
  [steps[i], steps[j]] = [steps[j], steps[i]];
  changed("Reorder", true);
}

function paintSteps() {
  const box = ui.stepsBox; if (!box) return; clear(box);
  if (!steps.length) { box.append(h("div", { class: "tiny" }, path ? "No steps yet - press Auto or pick a tool on the left." : "Open an image first.")); return; }
  steps.forEach((s, i) => {
    const o = opById[s.op] || { name: s.op };
    const row = h("div", { class: "step" + (s.uid === selUid ? " sel" : "") + (s.enabled ? "" : " off"), onclick: () => { selUid = s.uid; paintSteps(); paintParams(); } },
      h("input", { type: "checkbox", checked: s.enabled, title: "Switch this step on/off", onclick: (e) => e.stopPropagation(), onchange: (e) => { s.enabled = e.target.checked; changed(`${s.enabled ? "Enable" : "Disable"} ${o.name}`, true); } }),
      h("span", { class: "name" }, `${i + 1}. ${o.name}`),
      s.mask ? h("span", { class: "chip accent", title: "Uses a mask" }, masks[s.mask]?.name || "mask") : "",
      s.opacity < 0.999 ? h("span", { class: "chip" }, `${Math.round(s.opacity * 100)}%`) : "",
      h("button", { class: "small ghost", title: "Move up", onclick: (e) => { e.stopPropagation(); moveStep(s.uid, -1); } }, "↑"),
      h("button", { class: "small ghost", title: "Move down", onclick: (e) => { e.stopPropagation(); moveStep(s.uid, 1); } }, "↓"),
      h("button", { class: "small ghost", title: "Remove (Delete)", onclick: (e) => { e.stopPropagation(); removeStep(s.uid); } }, "✕"));
    box.append(row);
  });
}

function paintParams() {
  const box = ui.params; if (!box) return; clear(box);
  const s = steps.find((x) => x.uid === selUid);
  if (!s) return;
  const o = opById[s.op];
  if (!o) { box.append(h("div", { class: "warnbox" }, `Unknown tool ${s.op}`)); return; }
  const form = paramForm(o.params, s.params, (k, v) => { s.params[k] = v; changed(`${o.name}: ${k}`); }, { showAdvanced: state.advanced });
  const files = o.params.filter((p) => p.kind === "file").map((p) => h("div", { class: "field" }, h("label", {}, p.label),
    h("div", { class: "row" }, h("span", { class: "mono tiny", style: { flex: 1 } }, s.params[p.name] ? basename(s.params[p.name]) : "none"),
      h("button", { class: "small", onclick: async () => { const f = await chooseFiles({ multiple: false }); if (f) { s.params[p.name] = f[0]; changed(`${o.name}: ${p.label}`, true); paintParams(); } } }, "Choose..."))));
  const opac = h("input", { type: "range", min: 0, max: 100, value: Math.round((s.opacity ?? 1) * 100) });
  opac.addEventListener("change", () => { s.opacity = opac.value / 100; changed(`${o.name}: strength`, true); });
  const maskSel = h("select", { disabled: !o.mask_ok, onchange: (e) => { s.mask = e.target.value || null; changed(`${o.name}: mask`, true); } },
    h("option", { value: "" }, "Whole image"), ...Object.entries(masks).map(([id, m]) => h("option", { value: id, selected: s.mask === id }, m.name || id)));
  box.append(h("div", { class: "panel" },
    h("div", { class: "row" }, h("h3", { style: { margin: 0, flex: 1 } }, o.name), h("button", { class: "small ghost", onclick: () => { s.params = Object.fromEntries(o.params.map((p) => [p.name, p.default])); changed(`${o.name}: reset`, true); paintParams(); } }, "Reset")),
    h("div", { class: "tiny", style: { marginBottom: "8px" } }, o.description),
    o.id === "crop" ? h("button", { class: "small", style: { marginBottom: "8px" }, onclick: startCrop }, "Draw the crop on the image") : "",
    o.id === "deconvolution" ? h("button", { class: "small", style: { marginBottom: "8px" }, onclick: estimatePsf }, "Measure blur (PSF) from the image") : "",
    form, ...files,
    h("div", { class: "field" }, h("label", {}, "Strength of this step (blend with before)"), opac),
    o.mask_ok ? h("div", { class: "field" }, h("label", { "data-term": "Mask" }, "Apply to"), maskSel) : ""));
}

async function estimatePsf() {
  const r = await api.post("/api/process/psf", { path });
  const s = steps.find((x) => x.uid === selUid);
  if (s && s.op === "deconvolution") { s.params.fwhm = +r.fwhm.toFixed(2); s.params.psf = "gaussian"; changed("Deconvolution: measured blur", true); paintParams(); }
  toast(`Blur measured: FWHM ${fmtNum(r.fwhm, 2)} px (${r.how})`, "ok");
}

function startCrop() {
  const s = steps.find((x) => x.op === "crop"); if (!s) return;
  selUid = s.uid; cropEdit = true; ui.viewer.tool = "rect"; paintSteps(); paintParams();
  toast("Drag a rectangle on the picture to crop. (Showing the image before cropping.)");
  render(true);
}
function onSelectRect(r) {
  ui.viewer.tool = "pan";
  if (cropEdit) {
    cropEdit = false;
    const s = steps.find((x) => x.op === "crop");
    if (s) { s.params = { left: +r[0].toFixed(4), top: +r[1].toFixed(4), right: +r[2].toFixed(4), bottom: +r[3].toFixed(4) }; changed("Crop", true); paintParams(); }
  }
}

// -------------------------------------------------------------------- masks
function paintMasks() {
  const box = ui.masksBox; if (!box) return; clear(box);
  const types = ops?.mask_types || {};
  box.append(h("div", { class: "tiny" }, "A mask limits a step to part of the picture - e.g. sharpen only the planet, or reduce noise only in the dark background. Create one here, then pick it under 'Apply to' in a step."));
  for (const [id, m] of Object.entries(masks)) {
    const t = types[m.type] || { params: {} };
    const p = h("div", { class: "card", style: { marginTop: "8px" } });
    const name = h("input", { value: m.name || id, style: { flex: 1 } });
    name.addEventListener("change", () => { m.name = name.value; changed("Rename mask", true); paintSteps(); });
    p.append(h("div", { class: "row" }, name, h("span", { class: "chip" }, t.label || m.type),
      h("button", { class: "small" + (maskPreview === id ? " primary" : ""), onclick: () => { maskPreview = maskPreview === id ? null : id; render(true); paintMasks(); } }, maskPreview === id ? "Hide" : "Show"),
      h("button", { class: "small ghost", onclick: () => { delete masks[id]; steps.forEach((s) => { if (s.mask === id) s.mask = null; }); if (maskPreview === id) maskPreview = null; changed("Delete mask", true); paintMasks(); paintParams(); } }, "✕")));
    const specs = Object.entries(t.params || {}).map(([k, v]) => typeof v === "boolean" ? { name: k, label: prettyMask(k), kind: "bool", default: v }
      : { name: k, label: prettyMask(k), kind: "float", default: v, min: 0, max: k.includes("width") ? 100 : k === "sigma" ? 10 : k === "grow" ? 10 : 1, step: k.includes("width") || k === "sigma" || k === "grow" ? 0.5 : 0.01 });
    specs.push({ name: "feather", label: "Soft edge (px)", kind: "float", default: 0, min: 0, max: 50, step: 0.5 }, { name: "grow", label: "Grow / shrink (px)", kind: "float", default: 0, min: -30, max: 30, step: 1 },
      { name: "invert", label: "Invert (everything except)", kind: "bool", default: false });
    const vals = { ...(m.params || {}), feather: m.feather || 0, grow: m.grow || 0, invert: !!m.invert };
    p.append(paramForm(specs.filter((s, i, a) => a.findIndex((x) => x.name === s.name) === i), vals, (k, v) => {
      if (["feather", "grow", "invert"].includes(k) && !(k === "grow" && m.type === "stars" && k in (t.params || {}))) m[k] = v; else { m.params = m.params || {}; m.params[k] = v; }
      changed(`Mask ${m.name}: ${k}`);
    }));
    if (m.type === "brush") {
      const rad = h("input", { type: "range", min: 0.003, max: 0.2, step: 0.001, value: ui.viewer.brush.radius });
      rad.addEventListener("input", () => { ui.viewer.brush.radius = parseFloat(rad.value); });
      const hard = h("input", { type: "range", min: 0, max: 1, step: 0.05, value: ui.viewer.brush.hardness });
      hard.addEventListener("input", () => { ui.viewer.brush.hardness = parseFloat(hard.value); });
      p.append(h("div", { class: "row" },
        h("button", { class: "small" + (ui.viewer.tool === "brush" && ui.paintMask === id ? " primary" : ""), onclick: () => { ui.paintMask = id; ui.viewer.tool = ui.viewer.tool === "brush" ? "pan" : "brush"; maskPreview = id; render(true); paintMasks(); } }, "Paint"),
        h("button", { class: "small" + (ui.viewer.tool === "erase" && ui.paintMask === id ? " primary" : ""), onclick: () => { ui.paintMask = id; ui.viewer.tool = ui.viewer.tool === "erase" ? "pan" : "erase"; maskPreview = id; render(true); paintMasks(); } }, "Erase"),
        h("button", { class: "small ghost", onclick: () => { m.strokes = []; changed("Clear painting", true); } }, "Clear")),
      h("div", { class: "field" }, h("label", {}, "Brush size"), rad), h("div", { class: "field" }, h("label", {}, "Brush hardness"), hard),
      h("div", { class: "tiny" }, `${(m.strokes || []).length} stroke(s). Painting works on the picture as shown; put Crop/Rotate steps first.`));
    }
    box.append(p);
  }
  const typeSel = h("select", {}, ...Object.entries(types).map(([k, v]) => h("option", { value: k }, v.label)));
  box.append(h("div", { class: "row", style: { marginTop: "8px" } }, typeSel, h("button", { class: "small", onclick: () => {
    const id = "m" + newUid().slice(0, 5);
    const t = types[typeSel.value];
    masks[id] = { type: typeSel.value, name: t.label, params: { ...(t.params || {}) }, feather: 2, grow: 0, invert: false, strokes: [] };
    const s = steps.find((x) => x.uid === selUid);
    if (s && opById[s.op]?.mask_ok && !s.mask) s.mask = id;
    maskPreview = id;
    changed("New mask", true); paintMasks(); paintParams(); paintSteps();
  } }, "New mask")));
}
function prettyMask(k) { return { low: "From brightness", high: "To brightness", softness: "Softness", sigma: "Star detection threshold", grow: "Grow stars (px)", ring: "Only the edge (ring)", ring_width: "Ring width (px)" }[k] || k; }

function onPaint(stroke) {
  const id = ui.paintMask; const m = masks[id];
  if (!m) return;
  m.strokes = m.strokes || [];
  m.strokes.push({ ...stroke, points: stroke.points.map((p) => [+p[0].toFixed(4), +p[1].toFixed(4)]) });
  changed("Paint mask", true);
}

// ----------------------------------------------------------------- history
function paintHistory() {
  const box = ui.histBox; if (!box) return; clear(box);
  const p = state.project;
  if (!p || p.process?.base !== path || !p.process.history?.length) { box.append(h("div", { class: "tiny" }, "Every change is listed here once you start editing. Click an entry to go back to it.")); return; }
  const idx = p.process.index;
  box.append(h("div", { class: "listbox", style: { maxHeight: "260px" } }, ...p.process.history.map((e, i) => h("div", { class: "item tiny" + (i === idx ? " sel" : ""), style: { opacity: i > idx ? 0.5 : 1 }, onclick: () => jump(i) },
    h("span", { style: { flex: 1 } }, `${i + 1}. ${e.label}`), h("span", {}, (e.time || "").slice(11, 16)))).reverse()));
}

let commitTimer = null, pendingLabel = null;
function changed(label, immediate) {
  paintSteps();
  render(false);
  pendingLabel = label;
  clearTimeout(commitTimer);
  commitTimer = setTimeout(commit, immediate ? 50 : 900);
}
async function ensureProject() {
  const p = state.project;
  if (p && p.process?.base === path) return p;
  if (p && !p.process?.base && (p.sources || []).length === 0) return p;
  const np = await api.post("/api/projects", { name: basename(path).replace(/\.[^.]+$/, ""), sources: [] });
  return state.openProject(np);
}
async function commit() {
  if (!path) return;
  try {
    const p = await ensureProject();
    const proc = await api.post(`/api/projects/${p.id}/history/commit`, { steps, masks, label: pendingLabel || "Edit", base: path }, { quiet: true });
    p.process = proc;
    paintHistory();
  } catch (e) { toast("Could not save the edit history: " + e.message, "error"); }
}
async function moveHistory(action, body) {
  const p = state.project;
  if (!p || p.process?.base !== path) { toast("Nothing to undo yet", "warn"); return; }
  clearTimeout(commitTimer); if (pendingLabel) { await commit(); pendingLabel = null; }
  const proc = await api.post(`/api/projects/${p.id}/history/${action}`, body || {});
  p.process = proc;
  steps = JSON.parse(JSON.stringify(proc.steps)); masks = JSON.parse(JSON.stringify(proc.masks || {}));
  if (!steps.some((s) => s.uid === selUid)) selUid = steps[steps.length - 1]?.uid || null;
  paintSteps(); paintParams(); paintMasks(); paintHistory(); render(false);
}
const undo = () => moveHistory("undo");
const redo = () => moveHistory("redo");
const jump = (i) => moveHistory("jump", { index: i });

// ---------------------------------------------------------------- rendering
const renderSoon = debounce(() => doRender(), 120);
function render(now) { if (now) doRender(); else renderSoon(); }

function previewSize() {
  const r = ui.viewer.el.getBoundingClientRect();
  return Math.min(2200, Math.max(600, Math.round(Math.max(r.width, r.height) * (window.devicePixelRatio || 1))));
}

async function doRender() {
  if (!path) return;
  const seq = ++renderSeq;
  ui.viewer.setBusy(true, "Working...");
  const body = { path, steps, masks, max_size: previewSize(), guess: target === null };
  if (cropEdit) { const ci = steps.findIndex((s) => s.op === "crop"); body.upto = Math.max(0, ci); }
  if (maskPreview && masks[maskPreview]) body.mask_preview = { mask: maskPreview, step: steps.find((s) => s.mask === maskPreview)?.uid };
  try {
    const r = await api.post("/api/process/render", body, { quiet: true });
    if (seq !== renderSeq) return;
    await ui.viewer.setImage(r.image, { keepView: true, fullW: r.full_width, fullH: r.full_height });
    drawHistogram(ui.hist, r.histogram);
    notes = r.notes || [];
    ui.info.textContent = `${r.full_width} x ${r.full_height} px, preview at ${Math.round(r.scale * 100)}% - ${r.ms} ms${r.timings?.some((t) => !t.cached) ? "" : " (cached)"}${notes.length ? " - " + notes.join(" | ") : ""}`;
    if (r.target && target === null) { target = r.target; paintLibrary(); }
    if (ui.viewer.split !== null) updateBefore();
  } catch (e) {
    if (seq === renderSeq) { ui.info.textContent = e.message; toast(e.message, "error"); }
  } finally { if (seq === renderSeq) ui.viewer.setBusy(false); }
}

async function updateBefore() {
  const key = `${path}|${previewSize()}`;
  if (beforeKey === key) return;
  beforeKey = key;
  const r = await api.post("/api/process/render", { path, steps: [], max_size: previewSize() }, { quiet: true });
  await ui.viewer.setBefore(r.image);
}
async function toggleBefore() {
  if (!path) return;
  if (ui.viewer.split !== null) { ui.viewer.split = null; ui.beforeBtn.classList.remove("primary"); ui.viewer.draw(); return; }
  ui.viewer.split = 0.5; ui.beforeBtn.classList.add("primary");
  await updateBefore(); ui.viewer.draw();
  toast("Drag the line to compare. Press B again to turn off.");
}

async function detailPreview() {
  if (!path || !ui.viewer.img) return;
  const v = ui.viewer, r = v.el.getBoundingClientRect();
  const [ax, ay] = v.toImage(0, 0), [bx, by] = v.toImage(r.width, r.height);
  const iw = v.img.naturalWidth, ih = v.img.naturalHeight;
  const crop = [Math.max(0, ax / iw), Math.max(0, ay / ih), Math.min(1, bx / iw), Math.min(1, by / ih)];
  const big = h("div", { class: "tiny" }, "Processing at full resolution...");
  const m = modal({ title: "Full-resolution detail", body: big, wide: true, buttons: [{ label: "Close" }] });
  try {
    const res = await api.post("/api/process/render", { path, steps, masks, full: true, crop, max_size: 1800 });
    clear(big);
    big.append(h("img", { class: "astro", src: res.image, style: { maxWidth: "100%", display: "block", margin: "0 auto" } }),
      h("div", { class: "tiny" }, `${res.width} x ${res.height} px area at 100% (${res.ms} ms)`));
    lastFull = res;
  } catch { m.close(); }
}

async function saveFull() {
  if (!path) return;
  clearTimeout(commitTimer); await commit();
  try {
    const { job } = await api.post("/api/process/save", { path, project_id: state.project?.id, steps, masks });
    const r = await runJob(job, ui.save, { title: "Processing the full-size image" });
    await state.refreshProject();
    clear(ui.save);
    ui.save.append(h("div", { class: "okbox" }, h("b", {}, "Saved"), h("div", { class: "tiny mono" }, basename(r.fits)), h("div", { class: "tiny mono" }, basename(r.tiff)),
      ...(r.notes || []).map((n) => h("div", { class: "tiny" }, n)),
      h("button", { class: "primary", style: { marginTop: "6px" }, onclick: () => go("export", { paths: [r.tiff] }) }, "Export a JPEG/PNG for sharing →")));
  } catch { /* shown */ }
}
