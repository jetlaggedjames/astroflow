// Analyze: score every frame, graph it, choose what to keep (sliders, threshold, manual), spot problems.
import { api } from "../api.js";
import { QualityChart } from "../charts.js";
import { hint, runJob, sourceBox, statTiles } from "../common.js";
import { go, state } from "../state.js";
import { basename, clear, fmtDur, fmtNum, h, toast } from "../ui.js";

const METRIC_LABEL = {
  quality: "Quality score (0-100)", sharpness: "Sharpness", contrast: "Contrast", brightness: "Brightness", noise: "Noise",
  gradient: "Edge strength", drift: "Drift from centre (px)", clipped: "Clipped pixels (fraction)", area: "Object size (px²)",
  stars: "Stars detected", fwhm: "Star size FWHM (px)", eccentricity: "Star elongation", background: "Sky background", snr: "Signal to noise", trail: "Trails detected",
};
const FLAG_TEXT = {
  cloud: "Cloud / big brightness drop", trail: "Satellite or plane trail", clipped: "Over-exposed highlights", blurred: "Blurred (wind, focus, seeing)",
  elongated: "Stretched stars (tracking, wind, rotation)", "no-stars": "No stars found", partial: "Object partly out of frame", edge: "Object near the edge", unreadable: "Damaged frame",
};
const BAD_FLAGS = new Set(["cloud", "unreadable", "no-stars", "partial", "edge", "trail"]);

let el, ui = {}, rep = null, key = null, sources = [], chart, metric = "quality", sortBy = "time", pos = 0,
  sel = { mode: "percent", value: 25, include: [], exclude: [] }, kept = new Set(), keptFrames = [], selInfo = null;

export default {
  id: "analyze", title: "Analyze",
  create(root) { el = root; build(); },
  show(data) {
    if (data?.sources) { setSources(data.sources); if (data.run) analyze(); return; }
    if (!sources.length) {
      const p = state.project;
      if (p?.sources?.length) { setSources(p.sources); if (p.analysis?.key) loadReport(p.analysis.key, p.selection); }
      else if (state.lastRecording) setSources([state.lastRecording.path]);
    }
  },
  key(e) {
    if (!rep) return;
    if (e.key === "ArrowRight") { showFrame(Math.min(rep.frames.length - 1, pos + 1)); e.preventDefault(); }
    else if (e.key === "ArrowLeft") { showFrame(Math.max(0, pos - 1)); e.preventDefault(); }
    else if (e.key === "k" || e.key === "K") toggleKeep(true);
    else if (e.key === "x" || e.key === "X" || e.key === "Delete") toggleKeep(false);
  },
};

function build() {
  ui.side = h("div", { class: "side wide" });
  ui.center = h("div", { class: "center" });
  el.append(h("div", { class: "split" }, ui.side, ui.center));
  ui.src = sourceBox((s) => { setSources(s); rep = null; paint(); });
  ui.kind = h("select", {}, h("option", { value: "auto" }, "Detect automatically"), h("option", { value: "planetary" }, "Planet / Moon / Sun"), h("option", { value: "dso" }, "Deep sky (stars)"));
  ui.job = h("div");
  ui.summary = h("div");
  ui.keep = h("div");
  ui.next = h("div");
  ui.side.append(
    hint("Press ", h("b", {}, "Analyze"), ". Every frame gets a score; the best ones are kept for stacking. The suggested amount is usually right - just press ", h("b", {}, "Stack these frames"), "."),
    ui.src,
    h("div", { class: "panel" }, h("div", { class: "field" }, h("label", {}, "What is it?"), ui.kind),
      h("div", { class: "row" }, h("button", { class: "primary big", onclick: () => analyze(), style: { flex: 1 } }, "Analyze"),
        h("button", { class: "adv", onclick: () => analyze(true), "data-tip": "Score again even if a result is saved" }, "Re-analyze"))),
    ui.job, ui.summary, ui.keep, ui.next);

  ui.metric = h("select", { onchange: (e) => { metric = e.target.value; paintChart(); } });
  ui.sort = h("select", { onchange: (e) => { sortBy = e.target.value; paintChart(); } }, h("option", { value: "time" }, "In recording order"), h("option", { value: "quality" }, "Best first"));
  const cv = h("canvas", { class: "chart tall" });
  chart = new QualityChart(cv, {
    onThreshold: (v) => { if (metric !== "quality") return; sel = { ...sel, mode: "threshold", value: Math.round(v * 10) / 10 }; applySel(); },
    onPick: (i) => showFrame(sortBy === "quality" ? ui.order[i] : i),
  });
  ui.frameImg = h("img", { class: "astro", style: { maxWidth: "100%", maxHeight: "100%", objectFit: "contain", imageRendering: "auto" } });
  ui.frameInfo = h("div", { class: "row", style: { padding: "6px 8px" } });
  ui.best = h("div", { class: "row", style: { padding: "6px 8px", overflowX: "auto", flexWrap: "nowrap" } });
  ui.center.append(
    h("div", { class: "toolbar" }, h("b", {}, "Frame quality"), h("span", { class: "spacer" }), ui.metric, ui.sort),
    h("div", { style: { padding: "8px" } }, cv, h("div", { class: "tiny" }, "Blue = kept, grey = not kept, red = problem found. Click a bar to see that frame. Drag the dashed line to set a quality threshold.")),
    h("div", { style: { flex: 1, minHeight: "200px", background: "#000", display: "flex", alignItems: "center", justifyContent: "center", position: "relative", overflow: "hidden" } }, ui.frameImg),
    ui.frameInfo, ui.best);
  paint();
}

function setSources(s) { sources = [...s]; ui.src.set(sources); }

async function analyze(force = false) {
  if (!sources.length) { toast("Choose a recording or images first", "warn"); return; }
  try {
    const name = basename(sources[0]).replace(/\.[^.]+$/, "");
    const p = await state.projectFor(sources, name);
    const r = await api.post("/api/analyze", { sources, kind: ui.kind.value, force, project_id: p.id });
    if (r.job) await runJob(r.job, ui.job, { title: `Scoring ${sources.length > 1 ? sources.length + " files" : basename(sources[0])}` });
    await loadReport(r.key);
  } catch { /* shown */ }
}

async function loadReport(k, savedSel) {
  try { rep = await api.get(`/api/analyze/report/${k}`, { quiet: true }); } catch { rep = null; paint(); return; }
  key = k;
  if (rep.metrics.cx && rep.metrics.cy) {
    const med = (a) => { const v = a.filter((x) => x !== null).sort((x, y) => x - y); return v[Math.floor(v.length / 2)] || 0; };
    const mx = med(rep.metrics.cx), my = med(rep.metrics.cy);
    rep.metrics.drift = rep.metrics.cx.map((x, i) => (x === null || rep.metrics.cy[i] === null ? null : Math.hypot(x - mx, rep.metrics.cy[i] - my)));
  }
  const rec = rep.recommendation;
  sel = savedSel && savedSel.mode ? { include: [], exclude: [], ...savedSel } : { mode: "percent", value: rec.keep_percent, include: [], exclude: [] };
  if (!sources.length && rep.sources) setSources(rep.sources);
  clear(ui.metric);
  for (const m of ["quality", ...Object.keys(rep.metrics).filter((m) => METRIC_LABEL[m] && m !== "quality")])
    ui.metric.append(h("option", { value: m, selected: m === "quality" }, METRIC_LABEL[m]));
  metric = "quality";
  paint();
  await applySel();
  const best = rep.summary.best_frame;
  showFrame(Math.max(0, rep.frames.indexOf(best)));
}

async function applySel() {
  if (!rep) return;
  try { selInfo = await api.post("/api/analyze/select", { key, ...sel }, { quiet: true }); } catch { return; }
  keptFrames = selInfo.frames;
  const set = new Set(keptFrames);
  kept = new Set(); rep.frames.forEach((f, i) => { if (set.has(f)) kept.add(i); });
  if (state.project && state.project.sources && JSON.stringify(state.project.sources) === JSON.stringify(sources))
    api.patch(`/api/projects/${state.project.id}`, { selection: sel }, { quiet: true }).catch(() => {});
  paintChart(); paintKeep(); paintFrameInfo();
}

function paint() { paintSummary(); paintKeep(); paintChart(); paintNext(); if (!rep) { ui.frameImg.removeAttribute("src"); clear(ui.frameInfo); clear(ui.best); } }

function paintSummary() {
  const box = ui.summary; clear(box);
  if (!rep) return;
  const s = rep.summary, rec = rep.recommendation;
  const flagCounts = {};
  rep.flags.forEach((f) => f.forEach((x) => { flagCounts[x] = (flagCounts[x] || 0) + 1; }));
  box.append(h("div", { class: "panel" },
    h("h3", {}, rep.kind === "planetary" ? "Planet / Moon recording" : "Deep-sky frames"),
    statTiles([
      [s.frames.toLocaleString(), rep.n_frames && rep.n_frames !== s.frames ? `frames scored (of ${rep.n_frames.toLocaleString()})` : "frames"],
      s.duration_s ? [fmtDur(s.duration_s), "duration"] : null,
      [fmtNum(s.median_quality, 0), "median quality"],
      [s.flagged.toLocaleString(), "with problems"],
      s.median_fwhm ? [fmtNum(s.median_fwhm, 1), "median FWHM px", "Average star size. Smaller = sharper."] : null,
      s.median_stars ? [fmtNum(s.median_stars, 0), "stars per frame"] : null,
      s.max_drift_px !== undefined ? [fmtNum(s.max_drift_px, 0), "max drift px", "How far the object wandered in the frame."] : null,
    ]),
    h("div", { class: "okbox" }, h("b", {}, "Suggestion: "), rec.text),
    ...(rep.session_flags || []).map((f) => h("div", { class: "warnbox tiny" }, f.text)),
    Object.keys(flagCounts).length ? h("div", { class: "tiny", style: { marginTop: "6px" } }, h("b", {}, "Problems found: "),
      ...Object.entries(flagCounts).map(([k, n]) => h("span", { class: "chip " + (BAD_FLAGS.has(k) ? "bad" : "warn"), style: { margin: "2px" }, "data-tip": FLAG_TEXT[k] || k }, `${FLAG_TEXT[k] || k}: ${n}`)),
      h("div", {}, "Frames with red problems are never kept automatically.")) : ""));
}

function paintKeep() {
  const box = ui.keep; clear(box);
  if (!rep) return;
  const n = rep.frames.length;
  const modeSel = h("select", { onchange: (e) => { const m = e.target.value; sel = { ...sel, mode: m, value: m === "percent" ? rep.recommendation.keep_percent : m === "count" ? rep.recommendation.keep_count : m === "threshold" ? 50 : 100 }; paintKeep(); applySel(); } },
    h("option", { value: "percent", selected: sel.mode === "percent" }, "Best percentage"), h("option", { value: "count", selected: sel.mode === "count" }, "Best number of frames"),
    h("option", { value: "threshold", selected: sel.mode === "threshold" }, "Quality above a threshold"), h("option", { value: "all", selected: sel.mode === "all" }, "All usable frames"));
  const max = sel.mode === "percent" ? 100 : sel.mode === "count" ? n : 100;
  const rng = h("input", { type: "range", min: sel.mode === "count" ? 1 : 0.5, max, step: sel.mode === "count" ? 1 : 0.5, value: sel.value, style: { flex: 1 }, disabled: sel.mode === "all" });
  const num = h("input", { type: "number", min: 0, max, value: sel.value, style: { width: "90px" }, disabled: sel.mode === "all" });
  let t;
  const set = (v) => { sel = { ...sel, value: parseFloat(v) }; num.value = v; rng.value = v; clearTimeout(t); t = setTimeout(applySel, 150); };
  rng.addEventListener("input", () => set(rng.value)); num.addEventListener("change", () => set(num.value));
  box.append(h("div", { class: "panel" }, h("h3", {}, "What to keep"),
    h("div", { class: "field" }, h("label", {}, "Keep"), modeSel),
    h("div", { class: "row" }, rng, num, h("span", { class: "tiny" }, sel.mode === "percent" ? "%" : sel.mode === "count" ? "frames" : sel.mode === "threshold" ? "score" : "")),
    selInfo ? statTiles([[selInfo.count.toLocaleString(), "frames kept"], [`${fmtNum(selInfo.noise_reduction_x, 1)}x`, "less noise than 1 frame", "Stacking N frames reduces noise by about the square root of N."], [fmtNum(selInfo.mean_quality, 0), "average quality kept"]]) : "",
    sel.include.length || sel.exclude.length ? h("div", { class: "tiny" }, `Your manual choices: ${sel.include.length} forced in, ${sel.exclude.length} thrown out. `,
      h("button", { class: "small ghost", onclick: () => { sel = { ...sel, include: [], exclude: [] }; applySel(); } }, "Undo manual choices")) : "",
    h("button", { class: "small", style: { marginTop: "6px" }, onclick: () => { sel = { mode: "percent", value: rep.recommendation.keep_percent, include: [], exclude: [] }; paintKeep(); applySel(); } }, "Use the suggestion")));
}

function paintNext() {
  const box = ui.next; clear(box);
  if (!rep) return;
  box.append(h("button", { class: "primary big", style: { width: "100%" }, onclick: async () => {
    if (state.project) await api.patch(`/api/projects/${state.project.id}`, { selection: sel }).catch(() => {});
    go("stack", { sources, files: rep.files, analysis_key: key, selection: sel, frames: keptFrames, kind: rep.kind === "planetary" ? "lucky" : "dso" });
  } }, "Stack these frames →"));
}

function paintChart() {
  if (!rep) { chart.set([]); return; }
  let vals = rep.metrics[metric] || rep.quality;
  if (metric === "quality") vals = rep.quality;
  let order = rep.frames.map((_, i) => i);
  if (sortBy === "quality") order.sort((a, b) => (rep.quality[b] ?? -1) - (rep.quality[a] ?? -1));
  ui.order = order;
  const kp = new Set(order.map((o, i) => (kept.has(o) ? i : -1)).filter((i) => i >= 0));
  chart.set(order.map((o) => vals[o]), { kept: kp, flags: order.map((o) => rep.flags[o].filter((f) => BAD_FLAGS.has(f))),
    threshold: metric === "quality" ? (sel.mode === "threshold" ? sel.value : null) : null, label: METRIC_LABEL[metric] });
}

function showFrame(p) {
  if (!rep) return;
  pos = p;
  const frame = rep.frames[p];
  ui.frameImg.src = `/api/analyze/frame?${api.q({ key, index: frame, max: 1000 })}`;
  paintFrameInfo();
  paintBest();
}

function paintFrameInfo() {
  const box = ui.frameInfo; clear(box);
  if (!rep) return;
  const frame = rep.frames[pos];
  const isKept = kept.has(pos);
  const flags = rep.flags[pos];
  const rank = [...rep.quality].filter((q) => q > rep.quality[pos]).length + 1;
  box.append(h("button", { class: "small", onclick: () => showFrame(Math.max(0, pos - 1)) }, "◀"),
    h("button", { class: "small", onclick: () => showFrame(Math.min(rep.frames.length - 1, pos + 1)) }, "▶"),
    h("b", {}, `Frame ${(frame + 1).toLocaleString()}`),
    h("span", {}, `quality ${fmtNum(rep.quality[pos], 1)} (rank ${rank.toLocaleString()} of ${rep.frames.length.toLocaleString()})`),
    rep.files ? h("span", { class: "tiny mono" }, basename(rep.files[frame] || "")) : "",
    ...flags.map((f) => h("span", { class: "chip " + (BAD_FLAGS.has(f) ? "bad" : "warn") }, FLAG_TEXT[f] || f)),
    h("span", { class: "spacer" }),
    h("span", { class: "chip " + (isKept ? "ok" : "") }, isKept ? "Kept" : "Not kept"),
    h("button", { class: "small", onclick: () => toggleKeep(true), title: "K" }, "Keep"),
    h("button", { class: "small", onclick: () => toggleKeep(false), title: "X" }, "Throw out"));
}

function paintBest() {
  const box = ui.best; clear(box);
  if (!rep) return;
  const order = rep.frames.map((_, i) => i).filter((i) => !rep.flags[i].some((f) => BAD_FLAGS.has(f))).sort((a, b) => rep.quality[b] - rep.quality[a]).slice(0, 8);
  box.append(h("span", { class: "tiny" }, "Best frames:"));
  for (const i of order) box.append(h("img", { class: "astro", src: `/api/analyze/frame?${api.q({ key, index: rep.frames[i], max: 160 })}`, title: `Frame ${rep.frames[i] + 1}, quality ${fmtNum(rep.quality[i], 0)}`,
    style: { height: "70px", border: i === pos ? "2px solid var(--accent)" : "2px solid transparent", borderRadius: "4px", cursor: "pointer", background: "#000" }, onclick: () => showFrame(i) }));
}

function toggleKeep(keep) {
  if (!rep) return;
  const f = rep.frames[pos];
  const inc = new Set(sel.include), exc = new Set(sel.exclude);
  if (keep) { exc.delete(f); if (!kept.has(pos)) inc.add(f); } else { inc.delete(f); exc.add(f); }
  sel = { ...sel, include: [...inc], exclude: [...exc] };
  applySel();
}

