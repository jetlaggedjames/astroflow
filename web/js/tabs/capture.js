// Capture: connect the camera, live view, histogram, focus, record SER/FITS, live stacking, calibration frames.
import { api } from "../api.js";
import { drawHistogram, drawSparkline } from "../charts.js";
import { hint, runJob } from "../common.js";
import { go, state } from "../state.js";
import { $, basename, clear, confirmDialog, fmtBytes, fmtDur, fmtNum, h, modal, section, term, toast } from "../ui.js";
import { svgEl, svgText, Viewer } from "../viewer.js";

const ROI_PRESETS = [["full", "Full sensor"], ["1920x1080", "1920 x 1080"], ["1280x960", "1280 x 960"], ["800x600", "800 x 600"],
  ["640x480", "640 x 480 (planets)"], ["320x240", "320 x 240 (small planets, fastest)"]];
const SCENES = [["jupiter", "Jupiter"], ["saturn", "Saturn"], ["mars", "Mars"], ["moon", "Moon"], ["deepsky", "Deep sky (follows the simulated mount)"], ["star", "Single star (focus / collimation)"]];
const QUICK = {
  planet: { label: "Planet", tip: "Small frame in the middle, short exposure, high gain: many frames per second to beat the seeing.",
    roi: [640, 480], mode: "stream", controls: { exposure_ms: 8, gain: 300 }, bits: 8 },
  moon: { label: "Moon", tip: "Larger frame, very short exposure, low gain (the Moon is bright).",
    roi: [1920, 1080], mode: "stream", controls: { exposure_ms: 3, gain: 120 }, bits: 8 },
  deepsky: { label: "Deep sky", tip: "Full sensor, 10 second exposures, medium gain. Turn on Live stack to watch the picture build up.",
    roi: "full", mode: "exposures", exposure_s: 10, controls: { gain: 250 }, bits: 16 },
  find: { label: "Find & focus", tip: "Full sensor binned 2x2 and a quick exposure: a bright, fast view for centring and focusing.",
    roi: "full", bin: 2, mode: "stream", controls: { exposure_ms: 30, gain: 300 }, bits: 8 },
};

let el, viewer, ws, st = null, lastUi = 0, built = "", frameBusy = false, lastUrl = null;
let opts = { crosshair: true, collimation: false, showStack: false, histLog: false, stretch: "auto" };
let collim = null, collimTimer = null, focusBox = null;
const ui = {};

export default {
  id: "capture", title: "Capture", localOnly: true,
  create(root) { el = root; build(); },
  show() { connectWs(); refreshCameras(); },
  hide() { if (ws) { ws.onclose = null; ws.close(); ws = null; } stopCollimation(); },
  key(e) {
    if (e.key === "r" || e.key === "R") { toggleRecord(); e.preventDefault(); }
    else if (e.key === "s" || e.key === "S") { snapshot(); e.preventDefault(); }
    else if (e.key === "f" || e.key === "F") viewer.fitView();
    else if (e.key === "0") viewer.actualSize();
  },
};

function build() {
  viewer = new Viewer({ onSelectRect: onRect });
  viewer.overlays.push(drawOverlays);
  ui.side = h("div", { class: "side" });
  ui.toolbar = h("div", { class: "toolbar" });
  ui.bottom = h("div", { class: "row", style: { padding: "6px 8px", gap: "10px", alignItems: "stretch", borderTop: "1px solid var(--border)", background: "var(--bg2)", flexWrap: "nowrap" } });
  ui.hist = h("canvas", { class: "chart hist astro-chart", style: { height: "96px" } });
  ui.histInfo = h("div", { class: "tiny" });
  ui.focus = h("canvas", { class: "chart hist", style: { height: "96px" } });
  ui.bottom.append(
    h("div", { style: { flex: 1.3, minWidth: 0 } }, h("div", { class: "row tiny" }, term("Histogram"), h("span", { class: "spacer" }),
      h("label", { class: "check tiny" }, h("input", { type: "checkbox", onchange: (e) => { opts.histLog = e.target.checked; paintStats(); } }), "log scale")), ui.hist, ui.histInfo),
    h("div", { style: { flex: 1, minWidth: 0 } }, h("div", { class: "tiny" }, "Focus meter (live)"), ui.focus, h("div", { class: "tiny", id: "focusLabel" })));
  const center = h("div", { class: "center" }, ui.toolbar, viewer.el, ui.bottom);
  el.append(h("div", { class: "split" }, ui.side, center));
  buildToolbar();
  buildSide();
}

function buildToolbar() {
  const tb = ui.toolbar;
  ui.state = h("span", { class: "chip" }, "Not connected");
  ui.fps = h("span", { class: "tiny" });
  ui.recBtn = h("button", { class: "rec", onclick: toggleRecord, title: "Start / stop recording (R)" }, "● Record");
  tb.append(ui.state, ui.fps, h("span", { class: "spacer" }),
    h("select", { title: "Screen brightness only - never changes the recorded data", onchange: (e) => { opts.stretch = e.target.value; sendWs(); } },
      h("option", { value: "auto" }, "Display: auto brighten"), h("option", { value: "linear" }, "Display: as recorded"), h("option", { value: "strong" }, "Display: strong (faint objects)")),
    h("label", { class: "check tiny", "data-tip": "A cross in the middle of the frame to help centring" },
      h("input", { type: "checkbox", checked: true, onchange: (e) => { opts.crosshair = e.target.checked; viewer.draw(); } }), "Crosshair"),
    h("button", { class: "small", onclick: () => viewer.fitView(), title: "Fit (F)" }, "Fit"),
    h("button", { class: "small", onclick: () => viewer.actualSize(), title: "100% (0)" }, "100%"),
    h("span", { class: "sep" }),
    h("button", { onclick: snapshot, title: "Save the current frame (S)" }, "Snapshot"), ui.recBtn);
}

function buildSide() {
  const s = ui.side;
  clear(s);
  ui.hint = hint("Choose the camera and press ", h("b", {}, "Connect"), ". No telescope yet? The simulator lets you practise everything.");
  ui.warn = h("div");
  ui.connect = h("div", { class: "panel" });
  ui.quick = h("div", { class: "panel" });
  ui.controls = h("div", { class: "col" });
  ui.frame = h("div");
  ui.record = h("div");
  ui.last = h("div");
  ui.live = h("div");
  ui.calib = h("div");
  ui.tools = h("div");
  ui.sim = h("div");
  s.append(ui.hint, ui.warn, ui.connect, ui.quick,
    section("Exposure and gain", ui.controls),
    section("Frame size, binning, bit depth", ui.frame, { open: false }),
    section("Record", h("div", {}, ui.record, ui.last)),
    section("Live stack (deep sky)", ui.live, { open: false }),
    section("Calibration frames (darks, flats, bias)", ui.calib, { open: false, adv: true }),
    section("Focus, collimation and pointing tools", ui.tools, { open: false }),
    ui.sim);
  renderConnect();
  renderQuick();
  renderRecordForm();
  renderLive();
  renderCalib();
  renderTools();
  state.on("mode", () => { built = ""; update(true); });
}

// ------------------------------------------------------------------ connection
let cameras = [];
async function refreshCameras() {
  try { cameras = await api.get("/api/capture/cameras", { quiet: true }); } catch { cameras = [{ kind: "simulator", name: "Simulator" }]; }
  renderConnect();
}

function renderConnect() {
  const box = ui.connect; clear(box);
  const connected = st && st.camera;
  box.append(h("h3", {}, "Camera"));
  if (connected) {
    const c = st.camera;
    box.append(h("div", { class: "row" }, h("span", { class: "chip ok" }, "Connected"), h("b", {}, c.name)),
      h("div", { class: "tiny" }, `${c.sensor || ""} ${c.width} x ${c.height} px, ${c.pixel_um} µm pixels, ${c.color ? "colour (" + c.bayer + ")" : "mono"}`),
      st.temperature !== null && st.temperature !== undefined ? h("div", { class: "tiny" }, `Sensor temperature ${fmtNum(st.temperature, 1)} °C`) : "",
      h("div", { class: "row", style: { marginTop: "6px" } },
        st.state === "idle" ? h("button", { class: "primary", onclick: () => api.post("/api/capture/live", { on: true }) }, "Start live view") :
          h("button", { onclick: () => api.post("/api/capture/live", { on: false }), disabled: !!st.recording }, "Pause live view"),
        h("button", { onclick: async () => {
          if (st.recording && !(await confirmDialog("Disconnect camera?", "A recording is running. It will be stopped and saved first.", { ok: "Stop and disconnect" }))) return;
          await api.post("/api/capture/disconnect"); built = ""; } }, "Disconnect")));
    return;
  }
  const sel = h("select", { style: { width: "100%" } },
    ...cameras.map((c, i) => h("option", { value: i, disabled: !!c.disabled }, c.name)));
  const pref = cameras.findIndex((c) => c.kind === "playerone" && !c.disabled);
  if (pref >= 0) sel.value = String(pref);
  const scene = h("select", { style: { width: "100%" } }, ...SCENES.map(([v, l]) => h("option", { value: v, selected: v === (state.settings?.camera?.sim_scene || "jupiter") }, l)));
  const sceneRow = h("div", { class: "field" }, h("label", {}, "Simulated sky"), scene);
  const upd = () => { sceneRow.style.display = cameras[+sel.value]?.kind === "simulator" ? "" : "none"; };
  sel.addEventListener("change", upd);
  const err = cameras.find((c) => c.error);
  box.append(h("div", { class: "field" }, h("label", {}, "Camera"), sel), sceneRow,
    err ? h("div", { class: "warnbox tiny" }, "Player One library not found. Settings > Camera explains how to add it (the setup wizard can do it for you).") : "",
    h("div", { class: "row" }, h("button", { class: "primary big", onclick: async () => {
      const c = cameras[+sel.value];
      try {
        await api.post("/api/capture/connect", { kind: c.kind, camera_id: c.camera_id, scene: c.kind === "simulator" ? scene.value : undefined });
        toast("Camera connected", "ok"); built = "";
      } catch { /* toast shown */ }
    } }, "Connect"), h("button", { class: "ghost", onclick: refreshCameras }, "Search again")));
  upd();
}

// --------------------------------------------------------------- quick setups
function renderQuick() {
  const box = ui.quick; clear(box);
  box.append(h("h3", {}, "Quick setup"),
    h("div", { class: "tiny" }, "Starting points only - nothing changes until you press one. Then fine-tune exposure so the histogram peak sits around 50-70%."),
    h("div", { class: "grid2", style: { marginTop: "6px" } },
      ...Object.entries(QUICK).map(([k, q]) => h("button", { "data-tip": q.tip, onclick: () => applyQuick(k) }, q.label))));
}

async function applyQuick(k) {
  if (!st || !st.camera) { toast("Connect the camera first", "warn"); return; }
  if (st.recording) { toast("Stop recording first", "warn"); return; }
  const q = QUICK[k];
  try {
    if (q.roi === "full") await api.post("/api/capture/roi", { full: true, bin: q.bin || 1 });
    else await api.post("/api/capture/roi", { center_size: q.roi, bin: q.bin || 1 });
    await api.post("/api/capture/bitdepth", { bits: q.bits });
    await api.post("/api/capture/mode", { mode: q.mode, exposure_s: q.exposure_s });
    for (const [name, value] of Object.entries(q.controls)) await api.post("/api/capture/control", { name, value });
    if (k === "deepsky" && ui.fmt) ui.fmt.value = "fits";
    if (k !== "deepsky" && ui.fmt) ui.fmt.value = "ser";
    built = ""; toast(`${q.label} settings applied`, "ok");
    setTimeout(() => viewer.fitView(), 600);
  } catch { /* toast shown */ }
}

// -------------------------------------------------------------- live controls
function buildControls() {
  const box = ui.controls; clear(box);
  if (!st || !st.camera) { box.append(h("div", { class: "tiny" }, "Connect a camera to see its settings.")); return; }
  const modeSeg = h("div", { class: "seg", style: { width: "100%" } },
    h("button", { class: st.mode === "stream" ? "on" : "", style: { flex: 1 }, onclick: () => setMode("stream") }, "Video (planets, Moon)"),
    h("button", { class: st.mode === "exposures" ? "on" : "", style: { flex: 1 }, onclick: () => setMode("exposures") }, "Long exposures (deep sky)"));
  box.append(modeSeg);
  if (st.mode === "exposures") {
    const inp = h("input", { type: "number", min: 0.05, max: 900, step: 0.5, value: st.exposure_s, style: { width: "100%" } });
    inp.addEventListener("change", () => api.post("/api/capture/mode", { mode: "exposures", exposure_s: parseFloat(inp.value) }));
    ui.rotAdvice = h("div", { class: "tiny" });
    box.append(h("div", { class: "field" }, h("label", { "data-term": "Exposure" }, "Exposure per frame (seconds)"), inp), ui.rotAdvice);
    loadRotationAdvice();
  }
  for (const [name, c] of Object.entries(st.controls || {})) {
    if (name.startsWith("sim_")) continue;
    if (st.mode === "exposures" && name === "exposure_ms") continue;
    if (!state.advanced && ["usb_bandwidth", "frame_limit", "wb_r", "wb_b"].includes(name)) continue;
    box.append(controlRow(name, c));
  }
  // simulator knobs
  clear(ui.sim);
  const simCtl = Object.entries(st.controls || {}).filter(([n]) => n.startsWith("sim_"));
  if (simCtl.length) {
    const scene = h("select", { onchange: (e) => api.post("/api/capture/scene", { scene: e.target.value }).then(() => { setTimeout(() => viewer.fitView(), 500); }) },
      ...SCENES.map(([v, l]) => h("option", { value: v, selected: v === state.settings?.camera?.sim_scene }, l)));
    ui.sim.append(section("Simulator", h("div", {},
      h("div", { class: "tiny" }, "These only exist in the simulator: they imitate focusing, the air's turbulence and mirror alignment."),
      h("div", { class: "field" }, h("label", {}, "Scene"), scene),
      ...simCtl.map(([n, c]) => controlRow(n, c))), { open: false }));
  }
}

function controlRow(name, c) {
  const wrap = h("div", { class: "field", dataset: { ctl: name } });
  const tip = c.help ? { "data-tip": c.help } : {};
  const isExp = name === "exposure_ms";
  const num = h("input", { type: "number", value: c.value, step: c.step, min: c.min, max: c.max, disabled: !c.writable || c.auto });
  const toSlider = (v) => (isExp ? Math.log10(Math.max(v, c.min)) : v);
  const fromSlider = (v) => (isExp ? +Math.pow(10, v).toPrecision(3) : v);
  const rng = h("input", { type: "range", min: toSlider(c.min), max: toSlider(isExp ? Math.min(c.max, 60000) : c.max), step: isExp ? 0.01 : c.step, value: toSlider(c.value), disabled: !c.writable || c.auto });
  let t;
  const send = (v, auto) => { clearTimeout(t); t = setTimeout(() => api.post("/api/capture/control", { name, value: v, auto: !!auto }).catch(() => {}), 120); };
  rng.addEventListener("input", () => { num.value = fromSlider(parseFloat(rng.value)); send(parseFloat(num.value)); });
  num.addEventListener("change", () => { rng.value = toSlider(parseFloat(num.value)); send(parseFloat(num.value)); });
  const label = h("label", tip, h("span", {}, c.label), isExp ? h("span", { class: "val" }, expText(c.value)) : "");
  wrap.append(label, h("div", { class: "slider" }, rng, num));
  if (c.can_auto) wrap.append(h("label", { class: "check tiny" }, h("input", { type: "checkbox", checked: c.auto, onchange: (e) => send(parseFloat(num.value), e.target.checked) }), "Automatic"));
  wrap._update = (cc) => {
    if (wrap.contains(document.activeElement)) return;
    num.value = cc.value; rng.value = toSlider(cc.value);
    if (isExp) label.lastChild.textContent = expText(cc.value);
  };
  return wrap;
}
function expText(ms) { return ms >= 1000 ? `${fmtNum(ms / 1000, 2)} s` : `${fmtNum(ms, ms < 10 ? 2 : 1)} ms`; }

async function setMode(mode) {
  if (st?.recording) { toast("Stop recording before switching mode", "warn"); return; }
  await api.post("/api/capture/mode", { mode, exposure_s: mode === "exposures" ? (st.exposure_s > 0.2 ? st.exposure_s : 10) : undefined });
  if (ui.fmt) ui.fmt.value = mode === "exposures" ? "fits" : "ser";
  built = "";
}

async function loadRotationAdvice() {
  const tgt = st?.target;
  if (!ui.rotAdvice) return;
  if (!tgt) { ui.rotAdvice.textContent = "Tip: enter the target name under Record to get a field-rotation advice for the longest safe exposure."; return; }
  try {
    const p = await api.get("/api/catalog/position?" + api.q({ name: tgt }), { quiet: true });
    if (p.rotation && state.settings?.equipment?.mount_type !== "eq") {
      ui.rotAdvice.className = p.rotation.level === "ok" ? "okbox tiny" : p.rotation.level === "warn" ? "warnbox tiny" : "dangerbox tiny";
      ui.rotAdvice.textContent = `${p.name}: ${p.rotation.text}`;
    } else ui.rotAdvice.textContent = "";
  } catch { ui.rotAdvice.textContent = ""; }
}

// --------------------------------------------------------------- frame / ROI
function buildFrame() {
  const box = ui.frame; clear(box);
  if (!st || !st.camera) { box.append(h("div", { class: "tiny" }, "Connect a camera first.")); return; }
  const r = st.roi || {};
  const cur = `${r.w}x${r.h}`;
  const roiSel = h("select", { style: { width: "100%" } },
    ...ROI_PRESETS.map(([v, l]) => h("option", { value: v, selected: v === cur || (v === "full" && r.w === st.camera.width && r.h === st.camera.height) }, l)),
    ROI_PRESETS.some(([v]) => v === cur) || (r.w === st.camera.width && r.h === st.camera.height) ? "" : h("option", { value: "", selected: true }, `Custom ${r.w} x ${r.h}`));
  const binSel = h("select", {}, ...(st.camera.bins || [1, 2]).map((b) => h("option", { value: b, selected: b === r.bin }, `${b} x ${b}`)));
  const bitSel = h("select", {}, h("option", { value: 8, selected: st.bit_depth === 8 }, "8-bit (fastest)"), h("option", { value: 16, selected: st.bit_depth === 16 }, `16-bit (full ${st.camera.bit_depth}-bit data)`));
  const apply = async () => {
    const v = roiSel.value; const bin = parseInt(binSel.value);
    try {
      if (v === "full" || !v) await api.post("/api/capture/roi", { full: true, bin });
      else { const [w, hh] = v.split("x").map(Number); await api.post("/api/capture/roi", { center_size: [w, hh], bin }); }
      built = ""; setTimeout(() => viewer.fitView(), 500);
    } catch { /* toast */ }
  };
  roiSel.addEventListener("change", apply); binSel.addEventListener("change", apply);
  bitSel.addEventListener("change", () => api.post("/api/capture/bitdepth", { bits: parseInt(bitSel.value) }));
  box.append(
    h("div", { class: "field" }, h("label", { "data-term": "ROI" }, "Frame size (region of interest)"), roiSel),
    h("div", { class: "grid2" }, h("div", { class: "field" }, h("label", { "data-term": "Binning" }, "Binning"), binSel),
      h("div", { class: "field" }, h("label", {}, "Bit depth"), bitSel)),
    h("button", { class: "small", onclick: () => { viewer.tool = "rect"; viewer._rectMode = "roi"; toast("Drag a box on the image around the area to record"); } }, "Draw frame on image"),
    h("div", { class: "tiny", style: { marginTop: "6px" } }, `Current: ${r.out_w} x ${r.out_h} px at (${r.x}, ${r.y}), bin ${r.bin}. ` +
      `${fovText(r)}`));
}

function fovText(r) {
  const eq = state.settings?.equipment || {};
  const f = (eq.focal_length_mm || 1500) * ((eq.barlows || [])[eq.active_barlow || 0]?.factor || 1);
  const scale = 206.265 * (st.camera.pixel_um || 2.9) * (r.bin || 1) / f;
  return `Pixel scale ${scale.toFixed(2)}"/px, field ${(r.out_w * scale / 60).toFixed(1)}' x ${(r.out_h * scale / 60).toFixed(1)}'.`;
}

async function onRect(box) {
  const mode = viewer._rectMode; viewer.tool = "pan"; viewer._rectMode = null;
  if (mode === "roi") {
    const r = st.roi; const b = r.bin || 1;
    const x = r.x + box[0] * r.w, y = r.y + box[1] * r.h, w = (box[2] - box[0]) * r.w, hh = (box[3] - box[1]) * r.h;
    try { await api.post("/api/capture/roi", { x: Math.round(x), y: Math.round(y), w: Math.round(w), h: Math.round(hh), bin: b }); built = ""; setTimeout(() => viewer.fitView(), 500); } catch { /* toast */ }
  } else if (mode === "focus") {
    focusBox = [box[0], box[1], box[2] - box[0], box[3] - box[1]];
    await api.post("/api/capture/focusbox", { box: focusBox });
  }
}

// ------------------------------------------------------------------- record
function renderRecordForm() {
  const box = ui.record; clear(box);
  ui.target = h("input", { placeholder: "e.g. Jupiter, M42, Moon", style: { width: "100%" }, value: st?.target || "" });
  ui.target.addEventListener("change", async () => { await api.post("/api/capture/target", { target: ui.target.value }); loadRotationAdvice(); });
  ui.fmt = h("select", {}, h("option", { value: "ser" }, "SER video (planets, Moon)"), h("option", { value: "fits" }, "FITS image sequence (deep sky)"));
  ui.limitKind = h("select", {}, h("option", { value: "" }, "No limit (press Stop)"), h("option", { value: "frames" }, "Stop after N frames"), h("option", { value: "seconds" }, "Stop after N seconds"));
  ui.limitVal = h("input", { type: "number", value: 3000, min: 1, style: { width: "100px" } });
  const lim = h("div", { class: "row" }, ui.limitKind, ui.limitVal);
  const updLim = () => { ui.limitVal.style.display = ui.limitKind.value ? "" : "none"; ui.limitVal.value = ui.limitKind.value === "seconds" ? 90 : 3000; };
  ui.limitKind.addEventListener("change", updLim); updLim();
  ui.recStatus = h("div");
  box.append(
    h("div", { class: "field" }, h("label", {}, "Target name (used for folders and file names)"), ui.target),
    h("div", { class: "field" }, h("label", { "data-term": "SER" }, "Format"), ui.fmt),
    h("div", { class: "field" }, h("label", {}, "Length"), lim),
    h("div", { class: "tiny beg" }, "Planets: 60-120 s for Jupiter (it rotates), up to 3-4 minutes for Mars/Saturn. Moon: 1000-3000 frames per area."),
    ui.recStatus,
    h("div", { class: "row", style: { marginTop: "6px" } }, h("button", { class: "rec big", onclick: toggleRecord, style: { flex: 1 } }, "● Record (R)")));
}

async function toggleRecord() {
  if (!st || !st.camera) { toast("Connect the camera first", "warn"); return; }
  if (st.recording) { const r = await api.post("/api/capture/record/stop"); toast(`Saved ${r.frames || 0} frames`, "ok"); return; }
  if (!ui.target.value.trim()) {
    ui.target.value = st.mode === "exposures" ? "DeepSky" : "Planet";
    toast("Tip: type the target name before recording (used for file names)", "warn");
  }
  const body = { target: ui.target.value.trim(), format: ui.fmt.value };
  if (ui.limitKind.value === "frames") body.max_frames = parseInt(ui.limitVal.value);
  if (ui.limitKind.value === "seconds") body.max_seconds = parseFloat(ui.limitVal.value);
  if (body.format === "ser" && st.mode !== "stream") { toast("SER video needs Video mode. Switch mode or choose FITS.", "warn"); return; }
  try { await api.post("/api/capture/record/start", body); } catch { /* toast */ }
}

async function snapshot() {
  if (!st || !st.camera) { toast("Connect the camera first", "warn"); return; }
  try { const r = await api.post("/api/capture/snapshot", { format: "fits", target: ui.target.value }); toast(`Snapshot saved: ${basename(r.path)}`, "ok"); } catch { /* toast */ }
}

function paintRecord() {
  const box = ui.recStatus; clear(box);
  const r = st?.recording;
  const recBtns = el.querySelectorAll("button.rec");
  recBtns.forEach((b) => { b.classList.toggle("on", !!r); b.textContent = r ? "■ Stop recording (R)" : "● Record (R)"; });
  if (r) {
    const fps = r.elapsed_s ? r.frames / r.elapsed_s : 0;
    box.append(h("div", { class: "okbox" },
      h("b", {}, `Recording ${r.format.toUpperCase()}`),
      h("div", { class: "kv" },
        h("span", {}, "Frames"), h("span", {}, `${r.frames.toLocaleString()}${r.max_frames ? " of " + r.max_frames.toLocaleString() : ""}`),
        h("span", {}, "Time"), h("span", {}, `${fmtDur(r.elapsed_s)}${r.max_seconds ? " of " + fmtDur(r.max_seconds) : ""}`),
        h("span", {}, "Speed"), h("span", {}, `${fps.toFixed(1)} frames/s`),
        h("span", {}, "Written"), h("span", {}, fmtBytes(r.bytes)),
        h("span", {}, "Buffer"), h("span", {}, `${r.buffer} frames waiting${r.dropped ? `, ${r.dropped} dropped (disk too slow)` : ""}`),
        h("span", {}, "Disk free"), h("span", {}, fmtBytes(st.disk_free))),
      h("div", { class: "tiny mono" }, basename(r.path))));
  } else if (st?.disk_free !== undefined && st?.disk_free !== null) {
    box.append(h("div", { class: "tiny" }, `${fmtBytes(st.disk_free)} free on the capture drive`));
  }
  const last = st?.last_recording;
  clear(ui.last);
  if (last && !r) {
    state.lastRecording = last;
    ui.last.append(h("div", { class: "panel", style: { marginTop: "8px" } },
      h("b", {}, "Last recording"), h("div", { class: "tiny mono" }, basename(last.path)),
      h("div", { class: "tiny" }, `${last.frames.toLocaleString()} frames, ${fmtBytes(last.bytes)}, ${fmtDur(last.duration_s)} (${last.reason})${last.dropped ? `, ${last.dropped} dropped` : ""}`),
      h("div", { class: "row", style: { marginTop: "6px" } },
        h("button", { class: "primary", onclick: () => go("analyze", { sources: [last.path], run: true }) }, "Analyze it →"),
        h("button", { onclick: () => quickProcess(last.path), "data-tip": "Score, keep the best frames, stack and prepare an Auto edit in one go" }, "Quick process"))));
  }
}

async function quickProcess(path) {
  try {
    const { job } = await api.post("/api/workflow/quick", { sources: [path] });
    toast("Quick process started - see Tasks for progress");
    const { waitJob } = await import("../api.js");
    const res = await waitJob(job);
    await state.openProject(res.project_id);
    toast(`Stacked the best ${res.frames} frames`, "ok");
    go("process");
  } catch (e) { if (!e.cancelled) toast(e.message, "error"); }
}

// --------------------------------------------------------------- live stack
function renderLive() {
  const box = ui.live; clear(box);
  ui.liveInfo = h("div");
  box.append(h("div", { class: "tiny" }, "Stacks each new exposure onto the last, so a faint galaxy or nebula appears on screen within minutes. ",
    term("Field rotation", "Rotation"), " of an alt-az mount is corrected automatically."),
  h("div", { class: "row", style: { marginTop: "6px" } },
    h("button", { class: "primary", onclick: async () => {
      if (st?.mode !== "exposures") await setMode("exposures");
      await api.post("/api/capture/livestack/start", { calibrate: true }); opts.showStack = true; ui.showStack.checked = true; sendWs();
    } }, "Start live stack"),
    h("button", { onclick: async () => { const r = await api.post("/api/capture/livestack/stop", { save: true }); opts.showStack = false; ui.showStack.checked = false; sendWs(); if (r.saved) toast(`Live stack saved: ${basename(r.saved)}`, "ok"); } }, "Stop and save"),
    h("label", { class: "check tiny" }, ui.showStack = h("input", { type: "checkbox", onchange: (e) => { opts.showStack = e.target.checked; sendWs(); } }), "Show the stack (not single frames)")),
  ui.liveInfo);
}

function paintLive() {
  const ls = st?.livestack; const box = ui.liveInfo; clear(box);
  if (!ls) return;
  box.append(h("div", { class: ls.stacked === false && ls.frames ? "warnbox tiny" : "okbox tiny" },
    h("div", {}, ls.message), h("div", {}, `${ls.frames || 0} stacked, ${ls.skipped || 0} skipped${ls.stars ? `, ${ls.stars} stars` : ""}${ls.fwhm ? `, FWHM ${fmtNum(ls.fwhm, 1)} px` : ""}${ls.rotation_deg !== null && ls.rotation_deg !== undefined ? `, field turned ${fmtNum(ls.rotation_deg, 2)}°` : ""}`),
    ls.calibrated ? h("div", {}, "Using your dark/flat frames") : h("div", {}, "No matching darks/flats found (optional)")));
}

// -------------------------------------------------------------- calibration
const CAL_HELP = {
  dark: "Cover the telescope completely. Same exposure, gain and temperature as your light frames.",
  flat: "Point at an evenly lit surface (white T-shirt over the tube + dawn sky, or a tablet showing white). Do not touch focus.",
  flatdark: "Cover the telescope. Same exposure as your flats.",
  bias: "Cover the telescope. The shortest possible exposure is used automatically.",
};
function renderCalib() {
  const box = ui.calib; clear(box);
  const kind = h("select", {}, ...Object.keys(CAL_HELP).map((k) => h("option", { value: k }, { dark: "Darks", flat: "Flats", flatdark: "Flat darks", bias: "Bias" }[k])));
  const help = h("div", { class: "tiny" });
  const count = h("input", { type: "number", value: 30, min: 3, max: 500 });
  const exp = h("input", { type: "number", step: 0.001, min: 0.0001, placeholder: "current" });
  const upd = () => { help.textContent = CAL_HELP[kind.value]; exp.disabled = kind.value === "bias"; };
  kind.addEventListener("change", upd); upd();
  ui.calStatus = h("div");
  box.append(h("div", { class: "grid2" }, h("div", { class: "field" }, h("label", {}, "Type"), kind), h("div", { class: "field" }, h("label", {}, "How many"), count)),
    h("div", { class: "field" }, h("label", {}, "Exposure (s), empty = current"), exp), help,
    h("div", { class: "row", style: { marginTop: "6px" } },
      h("button", { class: "primary", onclick: async () => {
        if (!(await confirmDialog("Capture calibration frames", h("div", {}, h("p", {}, CAL_HELP[kind.value]), h("p", {}, "Ready?")), { ok: "Start" }))) return;
        try { await api.post("/api/capture/calibration/start", { kind: kind.value, count: parseInt(count.value), exposure_s: exp.value ? parseFloat(exp.value) : undefined }); } catch { /* */ }
      } }, "Start"),
      h("button", { onclick: () => api.post("/api/capture/calibration/cancel") }, "Cancel / clear")),
    ui.calStatus,
    h("div", { class: "tiny", style: { marginTop: "6px" } }, "Masters are kept in a calibration library and picked automatically by camera, gain, temperature and exposure when stacking."));
}
function paintCalib() {
  const c = st?.calibration; const box = ui.calStatus; clear(box);
  if (!c) return;
  box.append(h("div", { class: c.status === "error" ? "dangerbox tiny" : c.status === "done" ? "okbox tiny" : "panel tiny" },
    h("div", {}, `${c.kind}: ${c.done} of ${c.count} - ${c.status}`),
    c.flat_advice ? h("div", {}, `${c.flat_advice} (median ${(c.flat_level * 100).toFixed(0)}%)`) : "",
    c.master ? h("div", { class: "mono" }, `Master saved: ${basename(c.master)}`) : "", c.error ? h("div", {}, c.error) : ""));
}

// -------------------------------------------------------------------- tools
function renderTools() {
  const box = ui.tools; clear(box);
  ui.collimInfo = h("div", { class: "tiny" });
  box.append(
    h("h4", {}, "Focus"),
    h("div", { class: "tiny" }, "Turn the focuser slowly and watch the focus meter: for stars smaller is better, for planets/Moon higher is better."),
    h("div", { class: "row" },
      h("button", { class: "small", onclick: () => { viewer.tool = "rect"; viewer._rectMode = "focus"; toast("Drag a box around a star or a detailed area"); } }, "Measure in a box"),
      h("button", { class: "small", onclick: async () => { focusBox = null; await api.post("/api/capture/focusbox", { box: null }); viewer.draw(); } }, "Whole frame")),
    h("h4", { style: { marginTop: "10px" } }, term("Collimation")),
    h("div", { class: "tiny" }, "Centre a bright star, defocus it into a ring, then turn this on. The dark shadow should sit exactly in the middle."),
    h("label", { class: "check" }, h("input", { type: "checkbox", onchange: (e) => { opts.collimation = e.target.checked; e.target.checked ? startCollimation() : stopCollimation(); } }), "Collimation helper"),
    ui.collimInfo,
    h("h4", { style: { marginTop: "10px" } }, "Where am I pointing?"),
    h("div", { class: "tiny" }, "Plate-solves the current frame (needs stars) and tells you what is in view."),
    h("button", { class: "small", onclick: whereAmI }, "Solve current frame"));
}

function startCollimation() {
  stopCollimation();
  const tick = async () => {
    try { collim = await api.get("/api/capture/collimation", { quiet: true }); } catch { collim = null; }
    ui.collimInfo.textContent = collim ? `${collim.message}${collim.offset_percent !== undefined ? ` (offset ${collim.offset_percent}%)` : ""}` : "";
    viewer.draw();
  };
  tick(); collimTimer = setInterval(tick, 800);
}
function stopCollimation() { clearInterval(collimTimer); collimTimer = null; collim = null; if (viewer) viewer.draw(); }

async function whereAmI() {
  if (!st || !st.camera) { toast("Connect the camera first", "warn"); return; }
  const box = h("div");
  const m = modal({ title: "Where am I pointing?", body: box, buttons: [{ label: "Close" }] });
  try {
    const { job } = await api.post("/api/capture/solve", {});
    const r = await runJob(job, box, { title: "Plate solving the current frame..." });
    if (!r.solved) { box.append(h("div", { class: "warnbox" }, "Could not solve this frame. ", ...(r.notes || []).map((n) => h("div", { class: "tiny" }, n)))); return; }
    const mountOn = state.status && (await api.get("/api/mount/status", { quiet: true }).catch(() => null))?.connected;
    box.append(h("div", { class: "okbox" }, h("b", {}, `RA ${r.ra_str}   Dec ${r.dec_str}`),
      h("div", { class: "tiny" }, `Scale ${fmtNum(r.solution.scale_arcsec, 2)}"/px, rotation ${fmtNum(r.solution.rotation_deg, 1)}°, solved by ${r.solution.solver}`),
      r.mount_error_arcmin !== undefined ? h("div", {}, `The mount thinks it points ${fmtNum(r.mount_error_arcmin, 1)} arc-minutes away from this.`) : ""),
    r.objects.length ? h("div", {}, h("b", {}, "In view: "), r.objects.map((o) => o.label).join(", ")) : h("div", { class: "tiny" }, "No catalogued deep-sky object in this field."),
    mountOn ? h("button", { class: "primary", style: { marginTop: "8px" }, onclick: async () => {
      await api.post("/api/mount/sync", { ra: r.solution.ra, dec: r.solution.dec, label: "plate solve" }); toast("Mount synced to the plate-solve position", "ok"); m.close();
    } }, "Sync the mount to this position") : "");
  } catch { /* shown */ }
}

// ------------------------------------------------------------------ overlays
function drawOverlays(svg, v) {
  if (!v.img) return;
  const iw = v.img.naturalWidth, ih = v.img.naturalHeight;
  const [x0, y0] = v.toScreen(0, 0), [x1, y1] = v.toScreen(iw, ih);
  if (opts.crosshair) {
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, len = Math.min(x1 - x0, y1 - y0) * 0.08;
    for (const [a, b, c, d] of [[cx - len * 3, cy, cx - len, cy], [cx + len, cy, cx + len * 3, cy], [cx, cy - len * 3, cx, cy - len], [cx, cy + len, cx, cy + len * 3]])
      svg.append(svgEl("line", { x1: a, y1: b, x2: c, y2: d, stroke: "rgba(255,80,80,.8)", "stroke-width": 1.5 }));
    svg.append(svgEl("circle", { cx, cy, r: len, fill: "none", stroke: "rgba(255,80,80,.6)", "stroke-width": 1 }));
  }
  if (focusBox) {
    const [a, b] = v.toScreen(focusBox[0] * iw, focusBox[1] * ih), [c, d] = v.toScreen((focusBox[0] + focusBox[2]) * iw, (focusBox[1] + focusBox[3]) * ih);
    svg.append(svgEl("rect", { x: a, y: b, width: c - a, height: d - b, fill: "none", stroke: "#ffd23f", "stroke-width": 1.5, "stroke-dasharray": "5 3" }), svgText(a + 4, b - 6, "focus", { "font-size": 11 }));
  }
  if (opts.collimation && collim && collim.outer && st?.roi) {
    const f = iw / (st.roi.out_w || iw);
    const circ = (c, col) => { const [sx, sy] = v.toScreen(c[0] * f, c[1] * f); svg.append(svgEl("circle", { cx: sx, cy: sy, r: c[2] * f * v.zoom, fill: "none", stroke: col, "stroke-width": 2 }), svgEl("circle", { cx: sx, cy: sy, r: 3, fill: col })); };
    circ(collim.outer, "#4cff7a");
    if (collim.inner) circ(collim.inner, "#ffb347");
    const [tx, ty] = v.toScreen(collim.outer[0] * f, (collim.outer[1] - collim.outer[2]) * f);
    svg.append(svgText(tx - 120, ty - 10, collim.message, { "font-size": 13 }));
  }
}

// ---------------------------------------------------------------- live data
function connectWs() {
  if (ws) return;
  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws/preview`;
  ws = new WebSocket(url);
  ws.binaryType = "blob";
  ws.onopen = sendWs;
  ws.onmessage = async (m) => {
    if (typeof m.data === "string") { const d = JSON.parse(m.data); if (d.type === "status") { st = d.status; update(); } return; }
    if (frameBusy) return;
    frameBusy = true;
    const url2 = URL.createObjectURL(m.data);
    try { await viewer.setImage(url2, { keepView: true, fullW: st?.roi?.out_w, fullH: st?.roi?.out_h }); } catch { /* bad frame */ }
    if (lastUrl) URL.revokeObjectURL(lastUrl);
    lastUrl = url2; frameBusy = false;
  };
  ws.onclose = () => { ws = null; setTimeout(() => { if ($("#tab-capture").classList.contains("active")) connectWs(); }, 1500); };
}
function sendWs() {
  if (!ws || ws.readyState !== 1) return;
  const w = Math.min(1600, Math.round((viewer.el.clientWidth || 1000) * (window.devicePixelRatio || 1)));
  ws.send(JSON.stringify({ max_w: w, stretch: opts.stretch, fps: 12, livestack: opts.showStack }));
}

function update(force) {
  const now = performance.now();
  if (!force && now - lastUi < 250) return;
  lastUi = now;
  if (!st) return;
  const key = `${!!st.camera}|${st.mode}|${Object.keys(st.controls || {}).join(",")}|${JSON.stringify(st.roi)}|${st.bit_depth}|${state.mode}`;
  if (key !== built) {
    built = key; buildControls(); buildFrame(); renderConnect();
    ui.hint.style.display = st.camera ? "none" : "";
    if (st.target && ui.target && document.activeElement !== ui.target) ui.target.value = st.target;
  } else {
    for (const [name, c] of Object.entries(st.controls || {})) { const row = ui.controls.querySelector(`[data-ctl="${name}"]`) || ui.sim.querySelector(`[data-ctl="${name}"]`); row && row._update && row._update(c); }
  }
  const stateText = { disconnected: "Not connected", idle: "Connected (paused)", live: "Live", recording: "● Recording", exposing: st.recording ? "● Recording exposures" : "Exposing" }[st.state] || st.state;
  ui.state.textContent = stateText;
  ui.state.className = "chip " + (st.recording ? "bad" : st.state === "live" || st.state === "exposing" ? "ok" : "");
  ui.fps.textContent = st.camera ? (st.mode === "stream" ? `${fmtNum(st.fps, 1)} fps` : `${fmtNum(st.exposure_s, 1)} s exposures`) + (st.message ? ` - ${st.message}` : "") : "";
  clear(ui.warn);
  for (const w of st.warnings || []) ui.warn.append(h("div", { class: "warnbox tiny" }, w));
  if (st.camera_kind && !st.camera && st.state === "disconnected" && st.message) ui.warn.append(h("div", { class: "dangerbox" }, st.message));
  paintStats(); paintRecord(); paintLive(); paintCalib();
}

function paintStats() {
  const s = st?.stats;
  if (!s || !s.hist) { drawHistogram(ui.hist, null); ui.histInfo.textContent = ""; drawSparkline(ui.focus, []); return; }
  drawHistogram(ui.hist, s.hist, { log: opts.histLog, markers: [{ x: 0.5, color: "rgba(120,160,255,.4)" }, { x: 0.7, color: "rgba(120,160,255,.4)" }] });
  const peak = (s.max / s.full_scale) * 100;
  ui.histInfo.innerHTML = "";
  ui.histInfo.append(`Brightest ${peak.toFixed(0)}%, average ${((s.mean / s.full_scale) * 100).toFixed(1)}%  `,
    s.saturated ? h("span", { class: "chip bad" }, `Clipping: ${s.clipped_percent}% of pixels are pure white - lower exposure or gain`) :
      peak < 25 && st.mode === "stream" ? h("span", { class: "chip warn" }, "Quite dark - more exposure or gain would help") : h("span", { class: "chip ok" }, "No clipping"));
  const hist = (st.focus_history || []).map((f) => f.value);
  const last = (st.focus_history || []).slice(-1)[0];
  drawSparkline(ui.focus, hist, { label: last?.kind === "fwhm" ? "FWHM" : "Sharpness", better: last?.kind === "fwhm" ? "lower" : "higher" });
  const fl = $("#focusLabel"); if (fl) fl.textContent = last?.label || "";
}
