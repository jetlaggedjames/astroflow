// Library: every capture and result, searchable; previews; ratings/tags; safe clean-up and offloading.
import { api, uploadFiles } from "../api.js";
import { hint, previewUrl, reveal, runJob } from "../common.js";
import { go, state } from "../state.js";
import { basename, clear, debounce, fmtBytes, fmtDate, fmtNum, h, modal, pickPath, stars, toast } from "../ui.js";

const KIND_LABEL = { ser: "Video", fits: "FITS", tiff: "TIFF", xisf: "XISF", png: "PNG", jpg: "JPEG", raw: "Camera RAW", video: "Video", webp: "WebP" };
const STATUS_LABEL = { raw: "Raw capture", stacked: "Stacked", processed: "Processed", exported: "Exported", calibration: "Calibration" };
const SORTS = [["date_obs", "Date taken"], ["mtime", "Date modified"], ["name", "Name"], ["size", "Size"], ["target", "Target"], ["quality", "Quality"], ["rating", "Rating"], ["exposure", "Exposure"], ["gain", "Gain"], ["camera", "Camera"]];

let el, ui = {}, q = { text: "", kind: "", target: "", session: "", status: "", imagetype: "", min_rating: 0, tag: "", sort: "date_obs", desc: true },
  items = [], total = 0, totalSize = 0, sel = new Set(), lastClicked = null, view = "grid", focusId = null, loading = false;

export default {
  id: "library", title: "Library",
  create(root) { el = root; build(); state.on("library-changed", () => { refresh(); loadFacets(); }); },
  show(data) { if (data?.filter) Object.assign(q, data.filter); refresh(); loadFacets(); },
  key(e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "a") { e.preventDefault(); items.forEach((i) => sel.add(i.id)); paintSel(); }
    if (e.key === "Delete" && sel.size) trashSelected();
  },
};

function build() {
  ui.facets = h("div", { class: "side", style: { width: "270px", minWidth: "230px" } });
  ui.toolbar = h("div", { class: "toolbar" });
  ui.list = h("div", { class: "scroll", style: { flex: 1 } });
  ui.selbar = h("div", { class: "toolbar", style: { borderTop: "1px solid var(--border)", borderBottom: 0 } });
  ui.detail = h("div", { class: "side right" });
  el.append(h("div", { class: "split" }, ui.facets, h("div", { class: "center" }, ui.toolbar, ui.list, ui.selbar), ui.detail));
  buildToolbar();
  ui.detail.append(h("div", { class: "empty" }, "Click a file to see its details."));
}

function buildToolbar() {
  const tb = ui.toolbar;
  const search = h("input", { placeholder: "Search names, targets, tags, notes...", style: { flex: 1, minWidth: "180px" }, value: q.text });
  search.addEventListener("input", debounce(() => { q.text = search.value; refresh(); }, 250));
  const sort = h("select", { onchange: (e) => { q.sort = e.target.value; refresh(); } }, ...SORTS.map(([v, l]) => h("option", { value: v, selected: v === q.sort }, `Sort: ${l}`)));
  const dir = h("button", { class: "small", title: "Sort direction", onclick: () => { q.desc = !q.desc; dir.textContent = q.desc ? "↓" : "↑"; refresh(); } }, "↓");
  const viewBtn = h("button", { class: "small", onclick: () => { view = view === "grid" ? "list" : "grid"; viewBtn.textContent = view === "grid" ? "List view" : "Grid view"; paintItems(); } }, "List view");
  tb.append(search, sort, dir, viewBtn);
}

// ------------------------------------------------------------------ facets
async function loadFacets() {
  let f;
  try { f = await api.get("/api/library/facets", { quiet: true }); } catch { return; }
  const box = ui.facets; clear(box);
  const fileInput = h("input", { type: "file", multiple: true, style: { display: "none" }, onchange: async (e) => {
    const files = [...e.target.files]; if (!files.length) return;
    try { await uploadFiles(files, "added", (i, n, name) => { ui.scanBox.textContent = `Copying ${i}/${n}: ${name}`; }); toast(`Added ${files.length} file(s)`, "ok"); } finally { ui.scanBox.textContent = ""; refresh(); loadFacets(); }
  } });
  ui.scanBox = h("div", { class: "tiny" });
  box.append(
    hint("Everything you capture lands here automatically. ", h("b", {}, "Add folder"), " indexes older pictures without copying them. Select files, then use the buttons at the bottom."),
    h("div", { class: "row" },
      state.local ? h("button", { class: "small primary", onclick: addFolder }, "Add folder...") : "",
      h("button", { class: "small", onclick: scan, "data-tip": "Re-check all library folders for new, changed or removed files (fast: only reads headers)." }, "Rescan"),
      h("button", { class: "small", onclick: () => fileInput.click(), "data-tip": state.local ? "Copy files into AstroFlow's Uploads folder" : "Send files from this computer to the main computer" }, state.local ? "Import files..." : "Upload files..."),
      fileInput),
    ui.scanBox,
    h("div", { class: "tiny", style: { margin: "6px 0" } }, `${(f.totals.files || 0).toLocaleString()} files, ${fmtBytes(f.totals.size)}${f.totals.missing ? `, ${f.totals.missing} unavailable (drive unplugged?)` : ""}`),
    h("button", { class: "small", style: { width: "100%" }, onclick: cleanupAssistant }, "Free up space / clean up..."));
  const facet = (title, key, list, label = (x) => x.value) => {
    if (!list || !list.length) return;
    const d = h("details", { class: "panel", open: key === "target" || key === "session" || !!q[key] }, h("summary", {}, title));
    d.append(h("div", { class: "listbox", style: { maxHeight: "220px" } },
      h("div", { class: "item" + (!q[key] ? " sel" : ""), onclick: () => { q[key] = ""; refresh(); loadFacets(); } }, h("span", { style: { flex: 1 } }, "All")),
      ...list.map((x) => h("div", { class: "item" + (String(q[key]) === String(x.value ?? x.session) ? " sel" : ""), onclick: () => { q[key] = x.value ?? x.session; refresh(); loadFacets(); } },
        h("span", { style: { flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, label(x)), h("small", {}, x.count ?? x.files)))));
    box.append(d);
  };
  facet("Sessions (nights)", "session", f.sessions, (s) => `${s.night || "?"} - ${s.target || "mixed"} (${fmtBytes(s.size)})`);
  facet("Targets", "target", f.target);
  facet("Type", "kind", f.kind, (x) => KIND_LABEL[x.value] || x.value);
  facet("Status", "status", f.status, (x) => STATUS_LABEL[x.value] || x.value);
  facet("Frame type", "imagetype", f.imagetype);
  facet("Camera", "camera", f.camera);
  facet("Tags", "tag", f.tags);
  const rating = h("select", { onchange: (e) => { q.min_rating = parseInt(e.target.value); refresh(); } },
    ...[0, 1, 2, 3, 4, 5].map((n) => h("option", { value: n, selected: n === q.min_rating }, n ? `${"★".repeat(n)} or better` : "Any rating")));
  box.append(h("div", { class: "field" }, h("label", {}, "Rating"), rating));
}

async function addFolder() {
  const p = await pickPath({ title: "Add a folder to the library (files stay where they are)", dirs: true });
  if (!p) return;
  const roots = [...(state.settings.folders.library_roots || [])];
  if (!roots.includes(p)) roots.push(p);
  await state.saveSettings({ folders: { library_roots: roots } });
  await scan([p]);
}

async function scan(roots) {
  try {
    const { job } = await api.post("/api/library/scan", Array.isArray(roots) ? { roots } : {});
    const r = await runJob(job, ui.scanBox, { title: "Updating the library" });
    toast(`Library updated: ${r.files ?? 0} files checked, ${r.updated ?? 0} new or changed, ${r.missing ?? 0} missing (${r.seconds ?? 0} s)`, "ok");
  } catch { /* shown */ }
  refresh(); loadFacets();
}

// ------------------------------------------------------------------- items
async function refresh(more = false) {
  if (loading) return;
  loading = true;
  try {
    const res = await api.get("/api/library/query?" + api.q({ ...q, desc: q.desc, limit: 300, offset: more ? items.length : 0, min_rating: q.min_rating || "" }), { quiet: true });
    items = more ? items.concat(res.items) : res.items; total = res.total; totalSize = res.total_size;
    const ids = new Set(items.map((i) => i.id));
    for (const id of [...sel]) if (!ids.has(id)) sel.delete(id);
    paintItems();
  } catch { /* ignore */ } finally { loading = false; }
}

function paintItems() {
  const box = ui.list; clear(box);
  if (!items.length) {
    box.append(h("div", { class: "empty" }, h("div", {}, "No files here yet."),
      h("p", { class: "tiny" }, state.local ? "Record something in Capture, or press Add folder to index existing pictures." : "Use Upload files (left) or drag files onto this window.")));
    paintSel(); return;
  }
  if (view === "grid") {
    const grid = h("div", { class: "thumbgrid" });
    for (const it of items) grid.append(thumb(it));
    box.append(grid);
  } else {
    const t = h("table", { class: "list" });
    t.append(h("thead", {}, h("tr", {}, ...["Name", "Target", "Taken", "Type", "Size", "Frames", "Exposure", "Gain", "Quality", "Rating", "Status"].map((x) => h("th", {}, x)))));
    const tb = h("tbody");
    for (const it of items) {
      const tr = h("tr", { class: (sel.has(it.id) ? "sel " : "") + (it.missing ? "missing" : ""), onclick: (e) => click(e, it), ondblclick: () => openItem(it) },
        h("td", { class: "mono" }, it.name), h("td", {}, it.target || ""), h("td", {}, fmtDate(it.date_obs)), h("td", {}, KIND_LABEL[it.kind] || it.kind),
        h("td", {}, fmtBytes(it.size)), h("td", {}, it.frames > 1 ? it.frames.toLocaleString() : ""), h("td", {}, it.exposure ? `${fmtNum(it.exposure, it.exposure < 1 ? 3 : 1)} s` : ""),
        h("td", {}, it.gain ?? ""), h("td", {}, it.quality !== null && it.quality !== undefined ? fmtNum(it.quality, 0) : ""), h("td", {}, "★".repeat(it.rating || 0)), h("td", {}, STATUS_LABEL[it.status] || it.status));
      tr.dataset.id = it.id;
      tb.append(tr);
    }
    t.append(tb); box.append(t);
  }
  if (items.length < total) box.append(h("div", { class: "row", style: { justifyContent: "center", padding: "12px" } }, h("button", { onclick: () => refresh(true) }, `Show more (${items.length} of ${total.toLocaleString()})`)));
  paintSel();
}

function thumb(it) {
  const img = h("img", { loading: "lazy", class: "astro", src: `/api/library/thumb/${it.id}?size=320&m=${Math.round(it.mtime || 0)}`, alt: "" });
  img.addEventListener("error", () => { img.replaceWith(h("div", { class: "empty", style: { padding: "30px 0" } }, KIND_LABEL[it.kind] || it.kind)); });
  const card = h("div", { class: "thumb" + (sel.has(it.id) ? " sel" : ""), onclick: (e) => click(e, it), ondblclick: () => openItem(it), title: it.path },
    h("div", { class: "img" }, img, h("div", { class: "badges" },
      h("span", { class: "chip" }, KIND_LABEL[it.kind] || it.kind),
      it.status && it.status !== "raw" ? h("span", { class: "chip accent" }, STATUS_LABEL[it.status] || it.status) : "",
      it.solved ? h("span", { class: "chip ok", title: "Plate solved" }, "✓ solved") : "",
      it.offloaded_to ? h("span", { class: "chip", title: `Copy on ${it.offloaded_to}` }, "offloaded") : "",
      it.missing ? h("span", { class: "chip bad" }, "unavailable") : "")),
    h("div", { class: "meta" }, h("div", { class: "name" }, it.target ? `${it.target} - ${it.name}` : it.name),
      h("div", { class: "row tiny", style: { gap: "6px" } }, h("span", {}, fmtDate(it.date_obs || "").split(",")[0]), h("span", {}, fmtBytes(it.size)),
        it.frames > 1 ? h("span", {}, `${it.frames.toLocaleString()} fr`) : "", h("span", { class: "spacer" }), it.rating ? h("span", { class: "stars" }, "★".repeat(it.rating)) : "")));
  card.dataset.id = it.id;
  return card;
}

function click(e, it) {
  if (e.shiftKey && lastClicked !== null) {
    const a = items.findIndex((x) => x.id === lastClicked), b = items.findIndex((x) => x.id === it.id);
    for (let i = Math.min(a, b); i <= Math.max(a, b); i++) sel.add(items[i].id);
  } else if (e.ctrlKey || e.metaKey) {
    sel.has(it.id) ? sel.delete(it.id) : sel.add(it.id);
  } else { sel.clear(); sel.add(it.id); }
  lastClicked = it.id;
  paintSel();
  showDetail(it.id);
}

function selected() { return items.filter((i) => sel.has(i.id)); }

function paintSel() {
  for (const n of ui.list.querySelectorAll("[data-id]")) n.classList.toggle("sel", sel.has(+n.dataset.id));
  const s = selected();
  state.selection = s.map((i) => i.path);
  const bar = ui.selbar; clear(bar);
  const size = s.reduce((a, b) => a + (b.size || 0), 0);
  bar.append(h("span", { class: "tiny" }, s.length ? `${s.length} selected (${fmtBytes(size)})` : `${total.toLocaleString()} files, ${fmtBytes(totalSize)} - click to select, Ctrl/Shift for several`));
  bar.append(h("span", { class: "spacer" }));
  if (!s.length) { bar.append(h("button", { class: "small", onclick: () => { items.forEach((i) => sel.add(i.id)); paintSel(); } }, "Select all shown")); return; }
  const paths = s.map((i) => i.path);
  const vids = s.filter((i) => i.kind === "ser" || i.kind === "video");
  bar.append(
    h("button", { class: "small primary", onclick: () => go("analyze", { sources: paths, run: true }) }, "Analyze"),
    h("button", { class: "small", onclick: () => go("stack", { sources: paths }) }, "Stack"),
    s.length === 1 && !vids.length ? h("button", { class: "small", onclick: () => go("process", { path: paths[0] }) }, "Post-process") : "",
    s.length === 1 && !vids.length ? h("button", { class: "small", onclick: () => go("solve", { path: paths[0] }) }, "Plate solve") : "",
    !vids.length ? h("button", { class: "small", onclick: () => go("export", { paths }) }, "Export") : "",
    h("button", { class: "small", "data-tip": "Score, keep the best frames, stack and prepare an Auto edit in one go", onclick: () => quick(paths) }, "Quick process"),
    h("span", { class: "sep" }),
    h("button", { class: "small", onclick: trashSelected }, "Move to Recycle Bin..."),
    state.local ? h("button", { class: "small", onclick: offloadSelected }, "Offload to another drive...") : "",
    s.some((i) => i.offloaded_to) ? h("button", { class: "small", onclick: removeOffloaded }, "Remove offloaded originals...") : "",
    h("button", { class: "small ghost", onclick: () => { sel.clear(); paintSel(); } }, "Clear"));
}

async function quick(paths) {
  try {
    const { job } = await api.post("/api/workflow/quick", { sources: paths });
    toast("Quick process started (see Tasks)");
    const { waitJob } = await import("../api.js");
    const r = await waitJob(job);
    await state.openProject(r.project_id);
    toast(`Done: stacked ${r.frames} frames`, "ok");
    go("process");
  } catch (e) { if (!e.cancelled) toast(e.message, "error"); }
}

function openItem(it) {
  if (it.kind === "ser" || it.kind === "video") go("analyze", { sources: [it.path], run: true });
  else go("process", { path: it.path });
}

// ------------------------------------------------------------------ detail
async function showDetail(id) {
  focusId = id;
  let r;
  try { r = await api.get(`/api/library/file/${id}`, { quiet: true }); } catch { return; }
  if (focusId !== id) return;
  const box = ui.detail; clear(box);
  const isVideo = r.kind === "ser" || r.kind === "video";
  const img = h("img", { class: "astro", style: { width: "100%", background: "#000", borderRadius: "6px", minHeight: "120px" }, src: r.missing ? "" : previewUrl(r.path, 900, isVideo ? 0 : undefined) });
  box.append(h("h3", { style: { wordBreak: "break-all" } }, r.name), img);
  if (isVideo && r.frames > 1 && !r.missing) {
    const slider = h("input", { type: "range", min: 0, max: r.frames - 1, value: 0, style: { width: "100%" } });
    const lab = h("div", { class: "tiny" }, `Frame 1 of ${r.frames.toLocaleString()} (only the frame shown is read - fast even for huge files)`);
    slider.addEventListener("change", () => { img.src = previewUrl(r.path, 900, slider.value); lab.textContent = `Frame ${+slider.value + 1} of ${r.frames.toLocaleString()}`; });
    box.append(slider, lab);
  }
  if (r.missing) box.append(h("div", { class: "warnbox" }, "This file is not available right now (drive unplugged, renamed or moved).", r.offloaded_to ? ` A verified copy is on ${r.offloaded_to}.` : ""));
  const target = h("input", { value: r.target || "", style: { width: "100%" } });
  target.addEventListener("change", () => save({ target: target.value }));
  const tags = h("input", { value: r.tags || "", placeholder: "comma separated, e.g. best, good seeing", style: { width: "100%" } });
  tags.addEventListener("change", () => save({ tags: tags.value.split(",").map((t) => t.trim()).filter(Boolean) }));
  const notes = h("textarea", { rows: 3, style: { width: "100%" } }, r.notes || "");
  notes.addEventListener("change", () => save({ notes: notes.value }));
  async function save(p) { await api.patch(`/api/library/file/${id}`, p); const i = items.find((x) => x.id === id); if (i) Object.assign(i, p); }
  box.append(
    h("div", { class: "row", style: { margin: "6px 0" } }, stars(r.rating, (v) => save({ rating: v })), h("span", { class: "spacer" }),
      !state.local ? h("a", { class: "btn small", href: `/api/download?path=${encodeURIComponent(r.path)}`, download: r.name }, "Download") : h("button", { class: "small", onclick: () => reveal(r.path) }, "Show in folder")),
    h("div", { class: "field" }, h("label", {}, `Target${r.target_source ? ` (from ${r.target_source})` : ""}`), target),
    h("div", { class: "kv" },
      h("span", {}, "Type"), h("span", {}, `${KIND_LABEL[r.kind] || r.kind}${r.imagetype ? " - " + r.imagetype : ""}`),
      h("span", {}, "Status"), h("span", {}, STATUS_LABEL[r.status] || r.status),
      h("span", {}, "Size"), h("span", {}, `${fmtBytes(r.size)}${r.frames > 1 ? `, ${r.frames.toLocaleString()} frames` : ""}`),
      h("span", {}, "Pixels"), h("span", {}, `${r.width || "?"} x ${r.height || "?"}${r.bitdepth ? `, ${r.bitdepth}-bit` : ""}${r.bayer ? `, ${r.bayer}` : ""}`),
      h("span", {}, "Taken"), h("span", {}, fmtDate(r.date_obs) || "unknown"),
      h("span", {}, "Session"), h("span", {}, r.session || ""),
      r.camera ? h("span", {}, "Camera") : "", r.camera ? h("span", {}, r.camera) : "",
      r.exposure ? h("span", {}, "Exposure") : "", r.exposure ? h("span", {}, `${fmtNum(r.exposure, r.exposure < 1 ? 4 : 1)} s`) : "",
      r.gain !== null && r.gain !== undefined ? h("span", {}, "Gain") : "", r.gain !== null && r.gain !== undefined ? h("span", {}, r.gain) : "",
      r.temperature !== null && r.temperature !== undefined ? h("span", {}, "Sensor temp") : "", r.temperature !== null && r.temperature !== undefined ? h("span", {}, `${fmtNum(r.temperature, 1)} °C`) : "",
      r.quality !== null && r.quality !== undefined ? h("span", {}, "Quality") : "", r.quality !== null && r.quality !== undefined ? h("span", {}, fmtNum(r.quality, 0)) : "",
      r.solution ? h("span", {}, "Plate solved") : "", r.solution ? h("span", {}, `RA ${fmtNum(r.solution.ra, 3)}°, Dec ${fmtNum(r.solution.dec, 3)}°, ${fmtNum(r.solution.scale_arcsec, 2)}"/px`) : "",
      r.offloaded_to ? h("span", {}, "Offloaded to") : "", r.offloaded_to ? h("span", { class: "mono" }, r.offloaded_to) : "",
      h("span", {}, "File"), h("span", { class: "mono tiny", style: { wordBreak: "break-all" } }, r.path)),
    r.error ? h("div", { class: "warnbox tiny" }, r.error) : "",
    h("div", { class: "field", style: { marginTop: "8px" } }, h("label", {}, "Tags"), tags),
    h("div", { class: "field" }, h("label", {}, "Notes"), notes));
  if (r.history?.length) box.append(h("details", { class: "panel" }, h("summary", {}, "Processing history"), h("ol", { class: "tiny" }, ...r.history.map((x) => h("li", {}, x)))));
  const hdr = Object.entries(r.header || {});
  if (hdr.length) box.append(h("details", { class: "panel adv" }, h("summary", {}, `File header (${hdr.length} entries)`),
    h("div", { class: "kv tiny mono" }, ...hdr.flatMap(([k, v]) => [h("span", {}, k), h("span", { style: { wordBreak: "break-all" } }, typeof v === "object" ? JSON.stringify(v) : String(v))]))));
}

// ----------------------------------------------------------------- clean-up
function planDialog(plan, { title, okLabel, danger }) {
  return new Promise((resolve) => {
    const list = h("div", { class: "listbox", style: { maxHeight: "40vh" } },
      ...plan.files.map((f) => h("div", { class: "item tiny" }, f.raw ? h("span", { class: "chip bad" }, "RAW") : "", h("span", { class: "mono", style: { flex: 1, wordBreak: "break-all" } }, f.path), h("b", {}, fmtBytes(f.size)))));
    const ack = h("input", { type: "checkbox" });
    const body = h("div", {},
      h("p", {}, h("b", {}, plan.summary)),
      ...(plan.warnings || []).map((w) => h("div", { class: "dangerbox" }, w)),
      h("div", { class: "row" }, h("b", {}, `${plan.count} file(s)`), h("span", {}, `Total: ${fmtBytes(plan.total_size)}`)),
      list,
      h("label", { class: "check", style: { marginTop: "10px" } }, ack, "I have checked this list. Go ahead."),
      h("div", { class: "tiny" }, "Nothing happens until you confirm. This confirmation expires after 10 minutes."));
    const m = modal({
      title, body, wide: true,
      buttons: [{ label: "Cancel", value: false }, { label: okLabel, class: danger ? "danger" : "primary", value: true, action: () => { if (!ack.checked) { toast("Tick the box to confirm you checked the list", "warn"); return false; } } }],
      onClose: (v) => { if (v !== true) api.post("/api/cleanup/cancel", { token: plan.token }, { quiet: true }); resolve(v === true); },
    });
    return m;
  });
}

async function execPlan(plan, label) {
  const box = h("div");
  const m = modal({ title: label, body: box, buttons: [{ label: "Close" }] });
  try {
    const { job } = await api.post("/api/cleanup/execute", { token: plan.token });
    const r = await runJob(job, box, { title: label });
    box.append(h("div", { class: r.failed?.length ? "warnbox" : "okbox" },
      `${r.done} file(s) done${r.verified !== undefined ? `, ${r.verified} verified with checksums` : ""}${r.bytes ? `, ${fmtBytes(r.bytes)} freed` : ""}.`,
      ...(r.failed || []).map((f) => h("div", { class: "tiny" }, `${basename(f.path || f.name || "")}: ${f.error || f.reason || ""}`)),
      r.next ? h("div", {}, r.next) : ""));
    sel.clear(); refresh(); loadFacets();
    return r;
  } catch { m.close(); return null; }
}

async function trashSelected(includeRaw = false) {
  const ids = [...sel]; if (!ids.length) return;
  let plan;
  try { plan = await api.post("/api/cleanup/plan/trash", { ids, include_raw: includeRaw === true }, { quiet: true }); }
  catch (e) {
    if (e.status === 409 && /RAW/.test(e.message)) {
      const again = await new Promise((res) => modal({ title: "These include raw captures", body: h("div", {}, h("div", { class: "dangerbox" }, e.message), h("p", {}, "Raw captures are your original data. Offloading them to another drive is usually the better choice.")),
        buttons: [{ label: "Cancel", value: false }, { label: "Include raw captures anyway", class: "danger", value: true }], onClose: (v) => res(v === true) }));
      if (again) return trashSelected(true);
      return;
    }
    toast(e.message, "error"); return;
  }
  if (await planDialog(plan, { title: "Move to the Recycle Bin?", okLabel: `Move ${plan.count} file(s) to the Recycle Bin`, danger: true })) await execPlan(plan, "Moving to the Recycle Bin");
}

async function offloadSelected() {
  const ids = [...sel]; if (!ids.length) return;
  const dest = await pickPath({ title: "Choose the drive/folder to offload to (e.g. an external drive)", dirs: true });
  if (!dest) return;
  let plan;
  try { plan = await api.post("/api/cleanup/plan/offload", { ids, dest }); } catch { return; }
  if (!(await planDialog(plan, { title: "Offload to another drive?", okLabel: `Copy and verify ${plan.count} file(s)` }))) return;
  const r = await execPlan(plan, "Copying and verifying");
  if (r && r.done && await new Promise((res) => modal({ title: "Copies verified", body: h("p", {}, `${r.verified} file(s) were copied and verified with SHA-256 checksums. Do you also want to remove the originals from this computer? (They go to the Recycle Bin and the library remembers where the copies are.)`),
    buttons: [{ label: "Keep the originals", value: false }, { label: "Review removal...", class: "primary", value: true }], onClose: (v) => res(v === true) }))) {
    ids.forEach((i) => sel.add(i)); await refresh(); removeOffloaded(ids);
  }
}

async function removeOffloaded(idList) {
  const ids = Array.isArray(idList) ? idList : selected().filter((i) => i.offloaded_to).map((i) => i.id);
  if (!ids.length) return;
  let plan;
  try { plan = await api.post("/api/cleanup/plan/remove_offloaded", { ids }); } catch { return; }
  if (await planDialog(plan, { title: "Remove originals that have verified copies?", okLabel: `Move ${plan.count} original(s) to the Recycle Bin`, danger: true })) await execPlan(plan, "Removing offloaded originals");
}

async function cleanupAssistant() {
  const body = h("div");
  const m = modal({ title: "Free up space", body, wide: true, buttons: [{ label: "Close" }] });
  const space = await api.get("/api/cleanup/space").catch(() => null);
  if (space) {
    body.append(h("h3", {}, "Where the space goes"),
      h("div", { class: "grid2" },
        h("table", { class: "list" }, h("tbody", {}, ...space.by_kind.map((r) => h("tr", {}, h("td", {}, KIND_LABEL[r.kind] || r.kind), h("td", {}, `${r.files} files`), h("td", {}, h("b", {}, fmtBytes(r.size))))))),
        h("table", { class: "list" }, h("tbody", {}, ...space.by_status.map((r) => h("tr", {}, h("td", {}, STATUS_LABEL[r.status] || r.status), h("td", {}, `${r.files} files`), h("td", {}, h("b", {}, fmtBytes(r.size)))))))),
      h("div", { class: "tiny" }, ...Object.entries(space.drives).map(([root, d]) => h("div", {}, `${root}: ${fmtBytes(d.free)} free of ${fmtBytes(d.total)}`))));
  }
  const selectIds = (ids) => { sel.clear(); ids.forEach((i) => sel.add(i)); m.close(); refresh().then(paintSel); toast(`${ids.length} file(s) selected - review them, then choose what to do at the bottom`, "ok"); };
  const probs = await api.get("/api/cleanup/problems").catch(() => []);
  body.append(h("h3", { style: { marginTop: "12px" } }, "Failed or low-quality captures"));
  if (!probs.length) body.append(h("div", { class: "tiny" }, "None found."));
  else body.append(h("div", { class: "listbox" }, ...probs.map((p) => h("div", { class: "item tiny" }, p.raw ? h("span", { class: "chip warn" }, "raw") : "", h("span", { class: "mono", style: { flex: 1 } }, basename(p.path)), h("span", {}, p.reasons.join("; ")), h("b", {}, fmtBytes(p.size))))),
    h("button", { class: "small", onclick: () => { q = { ...q, text: "" }; selectIds(probs.map((p) => p.id)); } }, `Select these ${probs.length} (nothing is deleted yet)`));
  const dupBox = h("div");
  body.append(h("h3", { style: { marginTop: "12px" } }, "Duplicate files"), dupBox,
    h("button", { class: "small", onclick: async () => {
      try {
        const { job } = await api.post("/api/cleanup/duplicates");
        const groups = await runJob(job, dupBox, { title: "Comparing files (checksums)" });
        if (!groups.length) { dupBox.append(h("div", { class: "okbox tiny" }, "No duplicates found.")); return; }
        const extra = groups.flatMap((g) => g.files.filter((f) => f.id !== g.keep).map((f) => f.id));
        const reclaim = groups.reduce((a, g) => a + g.reclaimable, 0);
        dupBox.append(h("div", { class: "listbox" }, ...groups.map((g) => h("div", { class: "item tiny", style: { flexDirection: "column", alignItems: "flex-start" } },
          ...g.files.map((f) => h("div", { class: "mono" }, f.id === g.keep ? "KEEP  " : "extra ", f.path))))),
        h("button", { class: "small primary", onclick: () => selectIds(extra) }, `Select the ${extra.length} extra copies (${fmtBytes(reclaim)})`));
      } catch { /* shown */ }
    } }, "Find duplicates"));
  const ev = await api.get("/api/cleanup/events").catch(() => []);
  if (ev.length) body.append(h("details", { class: "panel", style: { marginTop: "12px" } }, h("summary", {}, "Clean-up history"),
    h("div", { class: "listbox tiny" }, ...ev.slice(0, 100).map((e) => h("div", { class: "item" }, h("span", {}, fmtDate(new Date(e.time * 1000).toISOString())), h("b", {}, e.action), h("span", { class: "mono", style: { flex: 1 } }, e.detail))))));
}
