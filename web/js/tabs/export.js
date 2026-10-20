// Export: save finished pictures in any format/size, with optional watermark, caption bar, labels, compass, scale bar.
import { api } from "../api.js";
import { chooseFiles, hint, reveal, runJob } from "../common.js";
import { state } from "../state.js";
import { basename, clear, debounce, fmtBytes, h, pickPath, promptDialog, section, toast } from "../ui.js";

const POSITIONS = [["top-left", "Top left"], ["top-center", "Top centre"], ["top-right", "Top right"], ["middle-left", "Middle left"], ["center", "Centre"], ["middle-right", "Middle right"],
  ["bottom-left", "Bottom left"], ["bottom-center", "Bottom centre"], ["bottom-right", "Bottom right"]];

let el, ui = {}, meta = null, items = [], folder = null, prevUrl = null,
  o = { format: "jpg", bits: 8, quality: 92, profile: "srgb", size: "original", width: 1920, height: 1080, percent: 50, fit: "contain", dpi: "",
    watermark: { enabled: false, type: "text", text: "", image_path: "", position: "bottom-right", size: 3, opacity: 0.6, margin: 2, color: "#ffffff" },
    caption: false, caption_position: "bottom", caption_text: "", annotate: false, compass: false, scale_bar: false };

export default {
  id: "export", title: "Export",
  async create(root) { el = root; build(); meta = await api.get("/api/export/options"); folder = meta.folder; try { const saved = JSON.parse(localStorage.getItem("af-export") || "null"); if (saved) o = { ...o, ...saved, watermark: { ...o.watermark, ...(saved.watermark || {}) } }; } catch { /* */ } paintOptions(); paintItems(); },
  show(data) {
    if (data?.paths) {
      const p = state.project;
      items = data.paths.map((path) => ({ path, project_id: p && p.process?.base === path && (p.process.steps || []).length ? p.id : undefined }));
      if (data.annotate) o.annotate = true;
    } else if (!items.length && state.project) {
      const p = state.project;
      if (p.process?.output) items = [{ path: p.process.output }];
      else if (p.process?.base) items = [{ path: p.process.base, project_id: (p.process.steps || []).length ? p.id : undefined }];
    }
    paintItems(); if (meta) paintOptions(); preview();
  },
};

function build() {
  ui.side = h("div", { class: "side wide" });
  ui.prev = h("img", { class: "astro", style: { maxWidth: "100%", maxHeight: "100%", objectFit: "contain" } });
  ui.prevInfo = h("div", { class: "tiny", style: { padding: "4px 8px" } });
  el.append(h("div", { class: "split" }, ui.side, h("div", { class: "center" },
    h("div", { class: "toolbar" }, h("b", {}, "Preview"), h("span", { class: "tiny" }, "(exactly what the file will look like, reduced to fit)")),
    h("div", { style: { flex: 1, background: "#111", display: "flex", alignItems: "center", justifyContent: "center", overflow: "hidden" } }, ui.prev), ui.prevInfo)));
  ui.items = h("div", { class: "panel" });
  ui.opts = h("div");
  ui.job = h("div");
  ui.done = h("div");
  ui.side.append(hint("Pick a format and size (JPEG at original size is perfect for sharing), then press ", h("b", {}, "Export"), ". Your original and processed masters are never changed."),
    ui.items, ui.opts, h("button", { class: "primary big", style: { width: "100%" }, onclick: run }, "Export"), ui.job, ui.done);
}

function paintItems() {
  const box = ui.items; clear(box);
  box.append(h("h3", {}, items.length > 1 ? `${items.length} images (batch)` : "Image"));
  if (!items.length) box.append(h("div", { class: "tiny" }, "Choose a processed image, or several for a batch export."));
  box.append(h("div", { class: "col" }, ...items.slice(0, 6).map((it, i) => h("div", { class: "row" },
    h("span", { class: "mono tiny", style: { flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, title: it.path }, basename(it.path)),
    it.project_id ? h("span", { class: "chip accent", "data-tip": "Your Post-process steps are applied at full resolution" }, "with edits") : "",
    h("button", { class: "small ghost", onclick: () => { items.splice(i, 1); paintItems(); preview(); } }, "✕"))),
  items.length > 6 ? h("div", { class: "tiny" }, `... and ${items.length - 6} more`) : ""),
  h("div", { class: "row", style: { marginTop: "6px" } },
    h("button", { class: "small", onclick: async () => { const f = await chooseFiles({ title: "Images to export" }); if (f) { items = items.concat(f.map((path) => ({ path }))); paintItems(); preview(); } } }, "Add images..."),
    state.selection.length ? h("button", { class: "small", onclick: () => { items = state.selection.map((path) => ({ path })); paintItems(); preview(); } }, `Library selection (${state.selection.length})`) : ""));
}

function set(k, v) { o[k] = v; save(); preview(); }
function setWm(k, v) { o.watermark = { ...o.watermark, [k]: v }; save(); preview(); }
function save() { try { localStorage.setItem("af-export", JSON.stringify(o)); } catch { /* private mode */ } }

function sel(value, options, onchange) {
  return h("select", { onchange: (e) => onchange(e.target.value) }, ...options.map(([v, l]) => h("option", { value: v, selected: String(v) === String(value) }, l)));
}
function num(value, onchange, attrs = {}) { const i = h("input", { type: "number", value, ...attrs }); i.addEventListener("change", () => onchange(i.value === "" ? "" : parseFloat(i.value))); return i; }
function chk(label, value, onchange, tip) { return h("label", { class: "check", "data-tip": tip }, h("input", { type: "checkbox", checked: !!value, onchange: (e) => onchange(e.target.checked) }), label); }
function field(label, control, extra = {}) { return h("div", { class: "field", ...extra }, h("label", {}, label), control); }

function paintOptions() {
  const box = ui.opts; clear(box);
  const f = meta.formats[o.format];
  if (!f.bits.includes(Number(o.bits))) o.bits = f.bits[0];
  const fmtBox = h("div", {},
    h("div", { class: "grid2" }, field("Format", sel(o.format, Object.entries(meta.formats).map(([k, v]) => [k, v.label]), (v) => { o.format = v; o.bits = meta.formats[v].bits[0]; save(); paintOptions(); preview(); })),
      field("Bit depth", sel(o.bits, f.bits.map((b) => [b, `${b}-bit${b === 32 ? " float" : ""}`]), (v) => set("bits", parseInt(v))))),
    f.quality ? field(`Quality (${o.quality})`, (() => { const r = h("input", { type: "range", min: 50, max: 100, value: o.quality }); r.addEventListener("change", () => { set("quality", parseInt(r.value)); paintOptions(); }); return r; })()) : "",
    o.format !== "fits" ? field("Colour profile", sel(o.profile, Object.entries(meta.profiles), (v) => set("profile", v)), { "data-tip": "sRGB for screens and the web. Adobe RGB for wide-gamut printing." }) : "",
    o.format === "fits" ? h("div", { class: "tiny" }, "FITS keeps the data for other astronomy software (no watermark or caption).") : "",
    (o.bits > 8 && (o.watermark.enabled || o.caption || o.annotate || o.compass || o.scale_bar)) ? h("div", { class: "tiny warnbox" }, "With overlays (watermark, caption, labels) the image is saved in 8-bit.") : "");
  const sizes = [...Object.entries(meta.sizes).map(([k, v]) => [k, v.label]), ["percent", "Percentage of original"], ["custom", "Custom size (pixels)"]];
  const sizeBox = h("div", {}, field("Size", sel(o.size, sizes, (v) => { o.size = v; save(); paintOptions(); preview(); })),
    o.size === "percent" ? field("Percent", num(o.percent, (v) => set("percent", v), { min: 1, max: 400 })) : "",
    o.size === "custom" ? h("div", { class: "grid2" }, field("Width (px, empty = keep ratio)", num(o.width, (v) => set("width", v), { min: 0 })), field("Height (px, empty = keep ratio)", num(o.height, (v) => set("height", v), { min: 0 }))) : "",
    o.size === "custom" ? field("If the shape differs", sel(o.fit, [["contain", "Fit inside (add black bars)"], ["cover", "Fill (crop the edges)"], ["stretch", "Stretch (distorts)"]], (v) => set("fit", v))) : "",
    !String(o.size).startsWith("print") ? field("Print resolution (DPI, optional)", num(o.dpi, (v) => set("dpi", v), { min: 0, max: 1200, placeholder: "none" }), { class: "field adv" }) : "");
  const wm = o.watermark;
  const wmBox = h("div", {}, chk("Add a watermark", wm.enabled, (v) => { setWm("enabled", v); paintOptions(); }),
    wm.enabled ? h("div", {},
      field("Type", sel(wm.type, [["text", "Text"], ["image", "Image / logo (PNG)"]], (v) => { setWm("type", v); paintOptions(); })),
      wm.type === "text" ? field("Text", (() => { const i = h("input", { value: wm.text, placeholder: "© Your Name", style: { width: "100%" } }); i.addEventListener("change", () => setWm("text", i.value)); return i; })())
        : h("div", { class: "row" }, h("span", { class: "mono tiny", style: { flex: 1 } }, wm.image_path ? basename(wm.image_path) : "No image chosen"),
          h("button", { class: "small", onclick: async () => { const f = await chooseFiles({ multiple: false, exts: [".png", ".jpg", ".jpeg", ".webp"] }); if (f) { setWm("image_path", f[0]); paintOptions(); } } }, "Choose...")),
      h("div", { class: "grid2" }, field("Position", sel(wm.position, POSITIONS, (v) => setWm("position", v))),
        wm.type === "text" ? field("Colour", (() => { const c = h("input", { type: "color", value: wm.color }); c.addEventListener("change", () => setWm("color", c.value)); return c; })()) : h("span")),
      h("div", { class: "grid3" }, field("Size %", num(wm.size, (v) => setWm("size", v), { min: 0.5, max: 50, step: 0.5 })), field("Opacity", num(wm.opacity, (v) => setWm("opacity", v), { min: 0, max: 1, step: 0.05 })),
        field("Margin %", num(wm.margin, (v) => setWm("margin", v), { min: 0, max: 20, step: 0.5 }))),
      watermarkPresets()) : "");
  const capBox = h("div", {}, chk("Caption bar (target, date, equipment, exposure)", o.caption, (v) => { set("caption", v); paintOptions(); }),
    o.caption ? h("div", {}, field("Where", sel(o.caption_position, [["bottom", "Below the picture"], ["top", "Above the picture"]], (v) => set("caption_position", v))),
      field("Extra text (optional)", (() => { const i = h("input", { value: o.caption_text, style: { width: "100%" }, placeholder: "e.g. 'Best of 5000 frames, seeing 3/5'" }); i.addEventListener("change", () => set("caption_text", i.value)); return i; })())) : "");
  const annBox = h("div", {}, h("div", { class: "tiny" }, "Needs a plate-solved image (Solve tab). Labels are drawn only on the exported copy."),
    chk("Label objects (galaxies, nebulae, stars)", o.annotate, (v) => set("annotate", v)), chk("Compass (north/east)", o.compass, (v) => set("compass", v)), chk("Scale bar", o.scale_bar, (v) => set("scale_bar", v)));
  const dest = h("div", {}, state.local ? h("div", { class: "row" }, h("span", { class: "mono tiny", style: { flex: 1, wordBreak: "break-all" } }, folder),
    h("button", { class: "small", onclick: async () => { const p = await pickPath({ title: "Save a copy to...", dirs: true, start: folder }); if (p) { folder = p; paintOptions(); } } }, "Change...")) :
    h("div", { class: "tiny" }, "Files are saved in the main computer's Exports folder; download links appear after exporting."));
  box.append(section("Format", fmtBox), section("Size", sizeBox), section("Watermark", wmBox, { open: wm.enabled }), section("Caption", capBox, { open: o.caption }),
    section("Labels, compass, scale bar", annBox, { open: o.annotate || o.compass || o.scale_bar }), section("Save to", dest));
}

function watermarkPresets() {
  const list = Object.keys(meta.watermarks || {});
  return h("div", { class: "row", style: { marginTop: "6px" } },
    list.length ? sel("", [["", "Saved watermarks..."], ...list.map((n) => [n, n])], (v) => { if (v) { o.watermark = { ...meta.watermarks[v], enabled: true }; save(); paintOptions(); preview(); } }) : "",
    h("button", { class: "small", onclick: async () => { const n = await promptDialog("Save watermark", "Name", "My watermark"); if (n) { meta.watermarks = await api.post("/api/export/watermarks", { name: n, watermark: o.watermark }); paintOptions(); toast("Watermark saved", "ok"); } } }, "Save for later"));
}

const preview = debounce(async () => {
  if (!items.length || !meta) { ui.prev.removeAttribute("src"); ui.prevInfo.textContent = ""; return; }
  ui.prevInfo.textContent = "Rendering preview...";
  try {
    const blob = await api.post("/api/export/preview", { item: items[0], options: { ...o, preview_width: 1200 } }, { quiet: true });
    if (prevUrl) URL.revokeObjectURL(prevUrl);
    prevUrl = URL.createObjectURL(blob);
    ui.prev.src = prevUrl;
    ui.prevInfo.textContent = `${basename(items[0].path)}${items.length > 1 ? ` (+${items.length - 1} more get the same settings)` : ""}`;
  } catch (e) { ui.prevInfo.textContent = "Preview failed: " + e.message; }
}, 350);

async function run() {
  if (!items.length) { toast("Choose an image first", "warn"); return; }
  try {
    const { job } = await api.post("/api/export", { items, options: o, folder: state.local ? folder : undefined });
    const r = await runJob(job, ui.job, { title: `Exporting ${items.length} image(s)` });
    clear(ui.done);
    ui.done.append(h("div", { class: "okbox" }, h("b", {}, `Saved ${r.files.length} file(s)`),
      ...r.files.map((f) => h("div", { class: "row tiny" }, h("span", { class: "mono", style: { flex: 1, wordBreak: "break-all" } }, basename(f.path)), h("span", {}, `${f.width} x ${f.height}, ${fmtBytes(f.bytes)}`),
        !state.local ? h("a", { class: "btn small", href: `/api/download?path=${encodeURIComponent(f.path)}`, download: basename(f.path) }, "Download") : "")),
      state.local ? h("button", { class: "small", style: { marginTop: "6px" }, onclick: () => reveal(r.files[0]?.path || r.folder) }, "Show in folder") : ""));
    toast("Export finished", "ok");
    state.emit("library-changed");
  } catch { /* shown */ }
}
