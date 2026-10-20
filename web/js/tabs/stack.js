// Stack: lucky imaging for planets/Moon, calibrated + rotation-aware deep-sky stacking, Moon mosaics.
import { api } from "../api.js";
import { chooseFiles, hint, previewUrl, reveal, runJob, sourceBox, statTiles } from "../common.js";
import { go, state } from "../state.js";
import { basename, clear, fmtBytes, fmtDur, fmtNum, h, paramForm, section, term, toast } from "../ui.js";

const LUCKY_SPECS = [
  { name: "ap_size", label: "Alignment point size (px)", kind: "choice", default: "0", choices: [["0", "Automatic"], ["24", "24 (tiny details)"], ["32", "32"], ["48", "48"], ["64", "64"], ["96", "96"], ["128", "128 (bad seeing)"]],
    help: "Size of the small patches that are lined up separately. Smaller follows the air's distortion better but needs more detail and signal." },
  { name: "upsample", label: "Output size", kind: "choice", default: "1", choices: [["1", "1x (normal)"], ["1.5", "1.5x"], ["2", "2x (with Barlow-limited data)"], ["3", "3x"]],
    help: "Stack onto a finer grid. Can reveal a little more detail when you have thousands of frames. Bigger files, slower." },
  { name: "upsample_method", label: "Upscaling method", kind: "choice", default: "interpolate", choices: [["interpolate", "Interpolate"], ["drizzle", "Drizzle"]], advanced: true,
    help: "Drizzle drops each frame's pixels onto the fine grid; needs many frames and some drift between them." },
  { name: "drizzle_pixfrac", label: "Drizzle drop size", kind: "float", default: 0.7, min: 0.3, max: 1, step: 0.05, advanced: true },
  { name: "derotate", label: "De-rotate (Jupiter/Saturn/Mars)", kind: "bool", default: false, help: "Jupiter spins once every 10 hours. Turn this on for recordings longer than about 90 seconds." },
  { name: "planet", label: "Planet", kind: "choice", default: "jupiter", choices: [["jupiter", "Jupiter"], ["saturn", "Saturn"], ["mars", "Mars"]] },
  { name: "pole_angle_deg", label: "North pole angle in image (deg)", kind: "float", default: 0, min: -180, max: 180, step: 1, advanced: true,
    help: "Rotation of the planet's axis in your picture (0 = north up). An alt-az mount turns the view over the night." },
  { name: "rotation_dir", label: "Spin direction in image", kind: "choice", default: "1", choices: [["1", "Features move right"], ["-1", "Features move left (mirrored)"]], advanced: true },
  { name: "multipoint", label: "Multi-point alignment", kind: "bool", default: true, advanced: true, help: "Align many small patches separately (strongly recommended)." },
  { name: "local_keep_percent", label: "Best % per alignment point", kind: "float", default: 60, min: 10, max: 100, step: 5, advanced: true,
    help: "Inside the kept frames, each patch uses only its own sharpest moments." },
  { name: "ap_min_brightness", label: "Skip dark sky below", kind: "float", default: 0.15, min: 0, max: 0.8, step: 0.01, advanced: true },
  { name: "align_mode", label: "Alignment", kind: "choice", default: "auto", choices: [["auto", "Automatic"], ["disk", "Planet disk (centre of light)"], ["surface", "Surface (Moon / Sun close-ups)"]], advanced: true },
  { name: "debayer_method", label: "Colour reconstruction", kind: "choice", default: "bilinear", choices: [["bilinear", "Bilinear (fast)"], ["vng", "VNG"], ["ea", "Edge-aware"]], advanced: true },
  { name: "normalize_brightness", label: "Even out brightness changes (thin cloud)", kind: "bool", default: true, advanced: true },
  { name: "reference_frames", label: "Frames for the reference (0 = auto)", kind: "int", default: 0, min: 0, max: 500, advanced: true },
  { name: "max_shift", label: "Max local shift px (0 = auto)", kind: "float", default: 0, min: 0, max: 60, advanced: true },
];
const DSO_SPECS = [
  { name: "registration_model", label: "Alignment model", kind: "choice", default: "similarity", choices: [["similarity", "Shift + rotation (alt-az, recommended)"], ["affine", "Affine (some distortion)"], ["homography", "Perspective"], ["poly2", "Polynomial (wide-field distortion)"]],
    help: "Alt-az mounts rotate the field between frames; every option corrects rotation." },
  { name: "rejection", label: "Outlier rejection", kind: "choice", default: "auto", choices: [["auto", "Automatic (by frame count)"], ["sigma", "Sigma clipping"], ["winsorized", "Winsorized sigma"], ["linear_fit", "Linear fit clipping"], ["percentile", "Percentile"], ["minmax", "Min/max"], ["none", "None (plain average)"]],
    help: "Removes satellite and plane trails, hot pixels and cosmic rays." },
  { name: "reject_low", label: "Reject low (sigma)", kind: "float", default: 3, min: 1, max: 6, step: 0.1, advanced: true },
  { name: "reject_high", label: "Reject high (sigma)", kind: "float", default: 3, min: 1, max: 6, step: 0.1, advanced: true },
  { name: "weighting", label: "Weighting", kind: "choice", default: "quality", choices: [["quality", "By frame quality"], ["noise", "By noise"], ["none", "Equal"]] },
  { name: "normalization", label: "Normalisation", kind: "choice", default: "additive_scaling", choices: [["additive_scaling", "Additive + scaling"], ["additive", "Additive"], ["none", "None"]], advanced: true },
  { name: "cosmetic", label: "Remove hot pixels", kind: "bool", default: true },
  { name: "hot_pixel_sigma", label: "Hot pixel threshold (sigma)", kind: "float", default: 6, min: 3, max: 15, step: 0.5, advanced: true },
  { name: "per_frame_gradient", label: "Remove gradients from each frame (light pollution, Moon)", kind: "bool", default: false },
  { name: "crop", label: "Crop ragged edges", kind: "bool", default: true },
  { name: "scale", label: "Output size", kind: "choice", default: "1", choices: [["1", "1x"], ["2", "2x (drizzle-style)"]], advanced: true },
  { name: "debayer_method", label: "Colour reconstruction", kind: "choice", default: "ea", choices: [["bilinear", "Bilinear"], ["vng", "VNG"], ["ea", "Edge-aware"]], advanced: true },
  { name: "max_stars", label: "Stars used for alignment", kind: "int", default: 400, min: 30, max: 2000, advanced: true },
  { name: "interpolation", label: "Resampling", kind: "choice", default: "lanczos", choices: [["lanczos", "Lanczos (sharpest)"], ["cubic", "Bicubic"], ["linear", "Bilinear"]], advanced: true },
];
const CAL_KINDS = [["dark", "Darks"], ["flat", "Flats"], ["flatdark", "Flat darks"], ["bias", "Bias"]];

let el, ui = {}, kind = "lucky", sources = [], analysisKey = null, selection = null, frames = null,
  lucky = {}, dso = {}, groups = [], mosaicModel = "similarity", estTimer = null;

export default {
  id: "stack", title: "Stack",
  create(root) { el = root; build(); state.on("mode", () => paintParams()); },
  show(data) {
    if (data) {
      if (data.sources) { sources = data.sources; ui.src.set(sources); }
      analysisKey = data.analysis_key || null; selection = data.selection || null; frames = data.frames || null;
      if (data.kind) kind = data.kind;
      else if (sources.length === 1 && /\.(ser|avi|mp4|mov|mkv)$/i.test(sources[0])) kind = "lucky";
      else if (sources.length > 1) kind = "dso";
      const files = data.files || sources;
      groups = [{ lights: frames && kind === "dso" ? frames.map((i) => files[i]).filter(Boolean) : sources, dark: "auto", flat: "auto", bias: "auto", flatdark: "auto" }];
      paintAll();
    } else if (!sources.length && state.project?.sources?.length) {
      sources = state.project.sources; ui.src.set(sources); analysisKey = state.project.analysis?.key || null; selection = state.project.selection || null;
      kind = state.project.analysis?.kind === "dso" ? "dso" : "lucky";
      groups = [{ lights: sources, dark: "auto", flat: "auto", bias: "auto", flatdark: "auto" }];
      paintAll();
    }
  },
};

function build() {
  ui.side = h("div", { class: "side wide" });
  ui.center = h("div", { class: "center scroll pad" });
  el.append(h("div", { class: "split" }, ui.side, ui.center));
  ui.src = sourceBox((s) => { sources = s; frames = null; analysisKey = null; selection = null; groups = [{ lights: s, dark: "auto", flat: "auto", bias: "auto", flatdark: "auto" }]; paintAll(); });
  ui.kindSeg = h("div", { class: "seg", style: { width: "100%" } });
  ui.sel = h("div");
  ui.params = h("div");
  ui.est = h("div", { class: "panel" });
  ui.run = h("div");
  ui.side.append(
    hint("Check the settings (the defaults are good), look at the time estimate, then press ", h("b", {}, "Stack"), "."),
    ui.src, h("div", { class: "panel" }, h("h3", {}, "What are you stacking?"), ui.kindSeg), ui.sel, ui.params, ui.est,
    h("button", { class: "primary big", style: { width: "100%" }, onclick: run }, "Stack"), ui.run);
  ui.result = h("div");
  ui.cal = h("div");
  ui.center.append(ui.result, section("Calibration library (master darks, flats, bias)", ui.cal, { open: false, adv: true }));
  paintAll();
  loadCal();
}

function paintAll() { paintKind(); paintSel(); paintParams(); estimate(); }

function paintKind() {
  clear(ui.kindSeg);
  for (const [k, l] of [["lucky", "Planet / Moon video"], ["dso", "Deep-sky frames"], ["mosaic", "Moon mosaic"]])
    ui.kindSeg.append(h("button", { class: kind === k ? "on" : "", style: { flex: 1 }, onclick: () => { kind = k; paintAll(); } }, l));
}

function paintSel() {
  const box = ui.sel; clear(box);
  if (kind === "mosaic") {
    box.append(h("div", { class: "panel tiny" }, "Choose the stacked (or processed) panel images as Files above. They are matched by their overlapping craters and blended into one large picture."));
    return;
  }
  if (kind === "lucky") {
    const p = h("div", { class: "panel" }, h("h3", {}, "Frames"));
    if (frames) p.append(h("div", {}, `${frames.length.toLocaleString()} frames chosen in Analyze.`), h("button", { class: "small", onclick: () => go("analyze", { sources }) }, "Change in Analyze"));
    else {
      const pct = h("input", { type: "number", min: 1, max: 100, value: selection?.value || 25, style: { width: "80px" } });
      pct.addEventListener("change", () => { selection = { mode: "percent", value: parseFloat(pct.value) }; estimate(); });
      p.append(h("div", { class: "row" }, "Keep the best", pct, "% of frames"),
        h("div", { class: "tiny" }, analysisKey ? "Uses the scores from Analyze." : "Frames are scored first (or run Analyze to choose them by hand)."));
    }
    box.append(p);
    return;
  }
  // deep sky: sessions with their calibration
  const p = h("div", { class: "panel" }, h("h3", {}, "Light frames and calibration"));
  groups.forEach((g, gi) => {
    const gBox = h("div", { class: "card", style: { marginBottom: "8px" } },
      h("div", { class: "row" }, h("b", {}, groups.length > 1 ? `Night ${gi + 1}` : "Lights"), h("span", { class: "tiny" }, `${expandCount(g.lights)} file(s)`), h("span", { class: "spacer" }),
        h("button", { class: "small", onclick: async () => { const f = await chooseFiles({ title: "Light frames for this night" }); if (f) { g.lights = f; paintSel(); estimate(); } } }, "Choose..."),
        groups.length > 1 ? h("button", { class: "small ghost", onclick: () => { groups.splice(gi, 1); paintSel(); estimate(); } }, "Remove") : ""));
    for (const [ck, cl] of CAL_KINDS) {
      if (!state.advanced && ck === "flatdark") continue;
      const v = g[ck];
      const mode = v === "auto" ? "auto" : !v || v === "none" ? "none" : "files";
      const s = h("select", { onchange: async (e) => {
        if (e.target.value === "files") { const f = await chooseFiles({ title: `${cl} (frames or one master file)` }); g[ck] = f && f.length ? (f.length === 1 ? f[0] : f) : "auto"; }
        else g[ck] = e.target.value;
        paintSel();
      } }, h("option", { value: "auto", selected: mode === "auto" }, "Automatic (calibration library)"), h("option", { value: "none", selected: mode === "none" }, "Don't use"),
      h("option", { value: "files", selected: mode === "files" }, mode === "files" ? `${Array.isArray(v) ? v.length + " files" : basename(v)}` : "Choose files..."));
      gBox.append(h("div", { class: "row", style: { marginTop: "4px" } }, h("span", { style: { width: "80px" }, "data-term": { dark: "Dark frame", flat: "Flat frame", bias: "Bias frame", flatdark: "Dark frame" }[ck] }, cl), s));
    }
    p.append(gBox);
  });
  p.append(h("button", { class: "small", onclick: () => { groups.push({ lights: [], dark: "auto", flat: "auto", bias: "auto", flatdark: "auto" }); paintSel(); } }, "+ Add another night (multi-session)"),
    h("div", { class: "tiny" }, "Automatic picks masters from the calibration library that match camera, gain, temperature and exposure. Without darks/flats the stack still works, with more noise and vignetting."));
  box.append(p);
}
function expandCount(l) { return Array.isArray(l) ? l.length : l ? 1 : 0; }

function paintParams() {
  const box = ui.params; clear(box);
  if (kind === "mosaic") {
    box.append(h("div", { class: "panel" }, h("div", { class: "field" }, h("label", {}, "Panel matching"),
      h("select", { onchange: (e) => { mosaicModel = e.target.value; } }, h("option", { value: "similarity" }, "Shift + rotation"), h("option", { value: "affine" }, "Affine"), h("option", { value: "homography" }, "Perspective")))));
    return;
  }
  const specs = kind === "lucky" ? LUCKY_SPECS : DSO_SPECS;
  const vals = kind === "lucky" ? lucky : dso;
  const form = paramForm(specs, vals, (k, v) => { vals[k] = v; if (k === "upsample" || k === "scale") estimate(); if (k === "derotate") paintParams(); }, { showAdvanced: state.advanced });
  if (kind === "lucky" && !vals.derotate) [...form.children].forEach((c) => { if (/Planet$|North pole|Spin direction/.test(c.textContent)) c.style.display = "none"; });
  box.append(section(kind === "lucky" ? "Lucky-imaging settings" : "Deep-sky settings", form));
  if (kind === "dso" && state.settings?.equipment?.mount_type !== "eq")
    box.append(h("div", { class: "tiny", style: { marginBottom: "8px" } }, term("Field rotation"), " between frames is measured and corrected for every frame."));
}

async function estimate() {
  clearTimeout(estTimer);
  estTimer = setTimeout(async () => {
    const box = ui.est; clear(box);
    const srcs = kind === "dso" ? groups.flatMap((g) => g.lights || []) : sources;
    if (!srcs.length) { box.append(h("div", { class: "tiny" }, "Choose files to see a time and memory estimate.")); return; }
    try {
      const n = kind === "lucky" ? (frames ? frames.length : undefined) : undefined;
      const e = await api.post("/api/stack/estimate", { sources: srcs, kind: kind === "mosaic" ? "dso" : kind, frames: n, params: kind === "lucky" ? lucky : dso }, { quiet: true });
      const lf = kind === "lucky" && !frames ? Math.round(e.frames * (selection?.value || 25) / 100) : e.frames;
      box.append(h("h3", {}, "Before you start"), statTiles([
        [lf.toLocaleString(), "frames"], [`${e.width} x ${e.height}`, "pixels"], [`~${fmtDur(kind === "lucky" && !frames ? e.seconds * ((selection?.value || 25) / 100) + 5 : e.seconds)}`, "estimated time"],
        [fmtBytes(e.peak_memory_bytes), "memory needed"], e.temp_disk_bytes ? [fmtBytes(e.temp_disk_bytes), "temporary disk"] : null]),
      e.fits_on_disk === false ? h("div", { class: "warnbox tiny" }, "Not much free disk space for temporary files: half-precision temporary files will be used.") : "",
      h("div", { class: "tiny" }, "Files are read piece by piece, so even very large recordings never have to fit in memory. You can cancel at any time."));
    } catch (err) { box.append(h("div", { class: "tiny" }, `Estimate unavailable: ${err.message}`)); }
  }, 300);
}

async function run() {
  const body = { kind, project_id: undefined, params: {} };
  let srcs;
  if (kind === "lucky") {
    if (!sources.length) { toast("Choose a recording first", "warn"); return; }
    srcs = sources;
    body.sources = sources; body.analysis_key = analysisKey;
    if (frames) body.frames = frames; else body.selection = selection || { mode: "percent", value: 25 };
    body.params = { ...lucky, ap_size: parseInt(lucky.ap_size || 0), upsample: parseFloat(lucky.upsample || 1), rotation_dir: parseInt(lucky.rotation_dir || 1) };
  } else if (kind === "dso") {
    if (!groups.some((g) => g.lights?.length)) { toast("Choose light frames first", "warn"); return; }
    srcs = groups.flatMap((g) => g.lights);
    body.groups = groups.filter((g) => g.lights?.length).map((g) => ({ ...g, ...Object.fromEntries(CAL_KINDS.map(([k]) => [k, g[k] === "none" ? null : g[k]])) }));
    body.params = { ...dso, scale: parseFloat(dso.scale || 1) };
  } else {
    if (sources.length < 2) { toast("Choose at least two panels", "warn"); return; }
    srcs = sources; body.sources = sources; body.params = { model: mosaicModel };
  }
  try {
    const name = basename(srcs[0]).replace(/\.[^.]+$/, "");
    const p = await state.projectFor(sources.length ? sources : srcs, name);
    body.project_id = p.id;
    const { job } = await api.post("/api/stack/run", body);
    const r = await runJob(job, ui.run, { title: "Stacking" });
    await state.refreshProject();
    showResult(r);
    toast("Stack finished", "ok");
  } catch { /* shown */ }
}

function showResult(r) {
  const box = ui.result; clear(box);
  const fits = r.fits;
  const img = h("img", { class: "astro", src: previewUrl(fits, 1400), style: { maxWidth: "100%", maxHeight: "60vh", display: "block", margin: "0 auto", background: "#000" } });
  const tiles = kind === "lucky" ? [
    [(r.frames || 0).toLocaleString(), "frames stacked"], [r.aps, "alignment points"], [`${r.ap_size}px`, "AP size"],
    [fmtNum(r.mean_global_shift_px, 1), "avg drift px"], [fmtNum(r.mean_local_shift_px, 2), "avg seeing shift px", "How much the air distorted small patches on average."], [`${r.seconds}s`, "time"]]
    : kind === "dso" ? [
      [`${r.frames_used} / ${r.frames_total}`, "frames used"], [r.rejection, "rejection"], [`${fmtNum(r.rejected_percent, 2)}%`, "pixels rejected"],
      r.max_field_rotation_deg !== undefined ? [`${fmtNum(r.max_field_rotation_deg, 2)}°`, "field rotation corrected"] : null, [`${r.seconds}s`, "time"]]
      : [[(r.placed || []).length, "panels placed"], [(r.missing || []).length, "could not be placed"]];
  box.append(h("div", { class: "panel" }, h("h3", {}, "Result"), img, statTiles(tiles),
    ...(r.notes || []).slice(-8).map((n) => h("div", { class: "tiny" }, n)),
    h("div", { class: "tiny mono", style: { marginTop: "6px" } }, `Saved: ${basename(r.fits)} (32-bit FITS) and ${basename(r.tiff)} (16-bit TIFF)`),
    h("div", { class: "beg hint", style: { marginTop: "8px" } }, "The stack looks soft and dark? That's normal - sharpening and stretching happen in Post-process."),
    h("div", { class: "row", style: { marginTop: "8px" } },
      h("button", { class: "primary big", onclick: () => go("process", { path: r.fits }) }, "Post-process →"),
      h("button", { onclick: () => go("solve", { path: r.fits }) }, "Plate solve"),
      h("button", { onclick: () => reveal(r.fits) }, "Show in folder"))));
  if (kind === "dso" && r.frames?.length && state.advanced) {
    const t = h("table", { class: "list" }, h("thead", {}, h("tr", {}, ...["Frame", "Stars", "Rotation", "Shift px", "Weight", "Status"].map((x) => h("th", {}, x)))),
      h("tbody", {}, ...r.frames.slice(0, 500).map((f) => h("tr", {}, h("td", { class: "mono" }, basename(f.path || "")), h("td", {}, f.stars ?? ""), h("td", {}, f.rotation_deg !== undefined ? fmtNum(f.rotation_deg, 2) + "°" : ""),
        h("td", {}, f.shift ? fmtNum(Math.hypot(...f.shift), 1) : ""), h("td", {}, f.weight !== undefined ? fmtNum(f.weight, 2) : ""), h("td", {}, f.status || (f.used === false ? "skipped" : "used"))))));
    box.append(section("Per-frame details", h("div", { class: "scroll", style: { maxHeight: "300px" } }, t), { open: false }));
  }
}

async function loadCal() {
  const box = ui.cal; clear(box);
  const list = await api.get("/api/stack/calibration", { quiet: true }).catch(() => []);
  if (!list.length) { box.append(h("div", { class: "tiny" }, "No masters yet. Capture > Calibration frames builds them for you, or they are added automatically when you stack with chosen dark/flat files.")); return; }
  box.append(h("table", { class: "list" }, h("thead", {}, h("tr", {}, ...["Type", "Camera", "Gain", "Exposure", "Temp", "Size", "Frames", "Made"].map((x) => h("th", {}, x)))),
    h("tbody", {}, ...list.map((e) => h("tr", {}, h("td", {}, e.kind), h("td", {}, e.key?.camera || ""), h("td", {}, e.key?.gain ?? ""), h("td", {}, e.key?.exposure !== undefined ? `${e.key.exposure}s` : ""),
      h("td", {}, e.key?.temperature !== null && e.key?.temperature !== undefined ? `${e.key.temperature}°C` : ""), h("td", {}, `${e.key?.width}x${e.key?.height}`), h("td", {}, e.frames ?? e.n_frames ?? ""), h("td", {}, (e.created || "").slice(0, 10)))))));
}
