// Settings: look and feel, equipment, location, folders, plate solving, camera, mount safety, second computer, GPU, support.
import { api, waitJob } from "../api.js";
import { state } from "../state.js";
import { clear, confirmDialog, debounce, h, modal, pickPath, section, toast } from "../ui.js";
import { cameraPanel, equipmentForm, folderForm, fovBox, locationForm, runWizard, solverPanel } from "../wizard.js";

let el, body;

export default {
  id: "settings", title: "Settings", icon: "",
  create(root) { el = root; body = h("div", { class: "pad scroll", style: { width: "100%", maxWidth: "980px", margin: "0 auto" } }); el.append(body); },
  async show(data) { await paint(); if (data?.section) document.getElementById(`set-${data.section}`)?.scrollIntoView({ behavior: "smooth" }); },
};

async function paint() {
  clear(body);
  const s = JSON.parse(JSON.stringify(state.settings));
  const saveLater = debounce((patch) => state.saveSettings(patch).then(() => toast("Saved", "ok", 1200)), 500);
  const local = state.local;

  // ---- appearance
  const seg = (value, opts, on) => h("div", { class: "seg" }, ...opts.map(([v, l]) => h("button", { class: value === v ? "on" : "", onclick: async (e) => { e.target.parentElement.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === e.target)); await on(v); } }, l)));
  const look = h("div", {},
    h("div", { class: "field" }, h("label", {}, "Mode"), seg(s.mode, [["beginner", "Beginner"], ["advanced", "Advanced"]], async (v) => { document.querySelector(`#modeSeg button[data-mode="${v}"]`).click(); })),
    h("div", { class: "field" }, h("label", {}, "Display"), seg(s.theme, [["dark", "Dark"], ["night", "Night (red)"], ["day", "Day (high contrast)"]], async (v) => { document.querySelector(`#themeSeg button[data-theme="${v}"]`).click(); })),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: !!s.touch_mode, onchange: async (e) => { await state.saveSettings({ touch_mode: e.target.checked }); document.documentElement.dataset.touch = e.target.checked ? "1" : "0"; } }), "Large controls (touch screens, gloves, outdoors)"),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: s.night_images !== false, onchange: async (e) => { await state.saveSettings({ night_images: e.target.checked }); document.documentElement.dataset.nightImages = e.target.checked ? "1" : "0"; } }), "In Night mode, show pictures in red too (best for dark adaptation)"));

  // ---- equipment
  const fov = h("div");
  const eqChanged = async () => { saveLater({ equipment: s.equipment }); clear(fov); try { fov.append((await fovBox(s.equipment)).el); } catch { /* */ } };
  const eq = h("div", {}, equipmentForm(s.equipment, eqChanged), fov);
  eqChanged();

  // ---- location
  const loc = locationForm(s.location, () => saveLater({ location: s.location }));

  // ---- folders
  const roots = h("div");
  const paintRoots = () => {
    clear(roots);
    roots.append(h("label", {}, "Extra library folders (indexed, never moved)"));
    (s.folders.library_roots || []).forEach((r, i) => roots.append(h("div", { class: "row" }, h("span", { class: "mono tiny", style: { flex: 1 } }, r),
      h("button", { class: "small ghost", onclick: async () => { s.folders.library_roots.splice(i, 1); await state.saveSettings({ folders: { library_roots: s.folders.library_roots } }); paintRoots(); } }, "Remove from library"))));
    roots.append(h("button", { class: "small", onclick: async () => { const p = await pickPath({ title: "Folder to index", dirs: true }); if (p) { s.folders.library_roots.push(p); await state.saveSettings({ folders: { library_roots: s.folders.library_roots } }); paintRoots(); } } }, "+ Add folder"),
      h("div", { class: "tiny" }, "Removing a folder here only stops indexing it - no files are touched."));
  };
  paintRoots();
  const folders = h("div", {}, folderForm(s.folders, () => saveLater({ folders: { captures: s.folders.captures, projects: s.folders.projects, exports: s.folders.exports } })), roots);

  // ---- solver
  const solverBox = h("div", {}, h("div", { class: "tiny" }, "Loading..."));
  solverPanel().then((p) => { clear(solverBox); solverBox.append(p); }).catch(() => {});
  const engine = h("select", { onchange: (e) => state.saveSettings({ solver: { engine: e.target.value } }) },
    ...[["auto", "Automatic (ASTAP, then built-in, then online if allowed)"], ["astap", "ASTAP only"], ["builtin", "Built-in only (wide fields)"], ["online", "Astrometry.net only (online)"]].map(([v, l]) => h("option", { value: v, selected: s.solver.engine === v }, l)));
  const pathRow = (key, label, dirs) => {
    const span = h("span", { class: "mono tiny", style: { flex: 1, wordBreak: "break-all" } }, s.solver[key] || "automatic");
    return h("div", { class: "field adv" }, h("label", {}, label), h("div", { class: "row" }, span,
      h("button", { class: "small", onclick: async () => { const p = await pickPath({ title: label, dirs }); if (p) { await state.saveSettings({ solver: { [key]: p } }); span.textContent = p; } } }, "Choose..."),
      h("button", { class: "small ghost", onclick: async () => { await state.saveSettings({ solver: { [key]: "" } }); span.textContent = "automatic"; } }, "Auto")));
  };
  const solver = h("div", {}, solverBox, h("hr"), h("div", { class: "field" }, h("label", {}, "Solver to use"), engine), pathRow("astap_path", "ASTAP program (astap_cli.exe)", false), pathRow("astap_db_dir", "Star database folder", true));

  // ---- camera
  const camBox = h("div", {}, h("div", { class: "tiny" }, "Loading..."));
  cameraPanel().then((p) => { clear(camBox); camBox.append(p); }).catch(() => {});
  if (state.status?.platform?.startsWith("win")) camBox.append(h("button", { class: "small", onclick: () => api.post("/api/mount/ascom/choose", { device: "Camera" }) }, "Choose ASCOM camera driver..."));

  // ---- mount safety
  const num = (v, min, max, on) => { const i = h("input", { type: "number", value: v, min, max }); i.addEventListener("change", () => on(parseFloat(i.value))); return i; };
  const mount = h("div", {},
    h("div", { class: "grid2" },
      h("div", { class: "field" }, h("label", { "data-tip": "The mount refuses GoTos below this altitude and stops hand moves there. Protects the tube, cables and anything near the base." }, "Lowest allowed altitude (degrees)"),
        num(s.mount.alt_limit_deg, 0, 45, (v) => state.saveSettings({ mount: { alt_limit_deg: v } }))),
      h("div", { class: "field" }, h("label", { "data-tip": "GoTo moves longer than this always ask for confirmation first." }, "Ask before moves larger than (degrees)"),
        num(s.mount.confirm_slew_deg, 5, 180, (v) => state.saveSettings({ mount: { confirm_slew_deg: v } })))),
    h("div", { class: "tiny" }, "AstroFlow never moves the telescope by itself; STOP (or Esc) works at any time; hand moves stop automatically if the connection drops."));

  // ---- second computer
  const net = h("div");
  const paintNet = async () => {
    clear(net);
    const n = await api.get("/api/network", { quiet: true }).catch(() => null);
    if (!n) return;
    net.append(h("p", { class: "tiny" }, "Work on your images from another computer or tablet on the same home network: open one of the addresses below in its web browser and enter the access code. The camera and mount stay controlled only from this computer."),
      local ? h("label", { class: "check" }, h("input", { type: "checkbox", checked: n.allow_lan, onchange: async (e) => { await state.saveSettings({ network: { allow_lan: e.target.checked } }); toast("Restart AstroFlow for this to take effect", "warn"); paintNet(); } }), "Allow other computers on my network") : "",
      n.allow_lan ? h("div", { class: "kv" }, h("span", {}, "Addresses"), h("div", {}, ...n.addresses.map((a) => h("div", { class: "mono" }, a))),
        local ? h("span", {}, "Access code") : "", local ? h("b", { class: "mono", style: { fontSize: "1.3em", letterSpacing: "3px" } }, n.access_code) : "") : h("div", { class: "tiny" }, "Currently only this computer can use AstroFlow."),
      local && n.allow_lan ? h("button", { class: "small", onclick: async () => {
        if (!(await confirmDialog("New access code", "Other computers will need the new code to sign in again.", { ok: "Make a new code" }))) return;
        await state.saveSettings({ network: { access_code: String(Math.floor(Math.random() * 1e6)).padStart(6, "0") } }); paintNet();
      } }, "Make a new access code") : "",
      h("div", { class: "tiny", style: { marginTop: "6px" } }, "Windows may ask whether to allow AstroFlow through the firewall the first time - choose Private networks only."));
  };
  paintNet();

  // ---- GPU / performance
  const gpu = h("div");
  api.get("/api/wizard/gpu_check", { quiet: true }).then((g) => {
    gpu.append(h("div", { class: g.cupy.available ? "okbox tiny" : "panel tiny" }, g.advice, g.gpu ? h("div", { class: "mono" }, g.gpu) : ""),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: s.gpu.enabled, onchange: (e) => state.saveSettings({ gpu: { enabled: e.target.checked } }) }), "Use the graphics card when available (falls back to the processor automatically)"),
      local && g.nvidia_driver && !g.cupy.available ? h("button", { class: "small", onclick: async () => {
        if (!(await confirmDialog("Install GPU acceleration", "Downloads the CuPy package for CUDA 12 (about 1 GB, needs internet once). Continue?", { ok: "Download and install" }))) return;
        try { const { job } = await api.post("/api/wizard/install_gpu"); toast("Installing in the background (see Tasks)"); const r = await waitJob(job); toast(r.note, "ok"); } catch (e) { toast(e.message, "error"); }
      } }, "Install GPU acceleration (~1 GB)") : "");
  }).catch(() => {});
  const perf = h("div", { class: "grid2" },
    h("div", { class: "field" }, h("label", { "data-tip": "How much memory stacking may use. 0 = automatic (a safe share of your RAM)." }, "Memory for stacking (GB, 0 = auto)"),
      num(s.performance.memory_budget_gb, 0, 512, (v) => state.saveSettings({ performance: { memory_budget_gb: v } }))),
    h("div", { class: "field" }, h("label", { "data-tip": "Processor threads for heavy work. 0 = automatic." }, "Processor threads (0 = auto)"),
      num(s.performance.threads, 0, 128, (v) => state.saveSettings({ performance: { threads: v } }))));

  // ---- support
  const support = h("div", {},
    h("div", { class: "row" },
      h("button", { onclick: async () => { const t = await api.get("/api/logs?lines=400"); modal({ title: "Log (newest at the bottom)", wide: true, body: h("pre", { class: "mono tiny", style: { whiteSpace: "pre-wrap", maxHeight: "60vh", overflow: "auto" } }, t), buttons: [{ label: "Close" }] }); } }, "Show the log"),
      h("a", { class: "btn", href: "/api/logs/download", download: "astroflow-bug-report.zip" }, "Save a bug report file"),
      local ? h("button", { onclick: () => runWizard() }, "Run the setup wizard again") : ""),
    h("p", { class: "tiny" }, "The bug report contains the log and your settings (the network access code and API keys are removed). Attach it to an email when asking for help."),
    h("p", { class: "tiny" }, `${state.status?.app || "AstroFlow"} - open-source components and their licences are listed in THIRD_PARTY.md next to the program.`));

  body.append(h("h2", {}, "Settings"),
    section("Look and feel", look, { id: "set-look" }),
    section("Telescope and camera", eq, { id: "set-equipment" }),
    section("Location", loc, { id: "set-location" }),
    local ? section("Folders", folders, { id: "set-folders" }) : "",
    section("Plate solving", solver, { id: "set-solver" }),
    local ? section("Camera driver", camBox, { id: "set-camera" }) : "",
    local ? section("Mount safety", mount, { id: "set-mount" }) : "",
    section("Second computer (home network)", net, { id: "set-network" }),
    section("Graphics card and performance", h("div", {}, gpu, perf), { id: "set-gpu" }),
    section("Support", support, { id: "set-support" }));
}
