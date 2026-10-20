// AstroFlow app shell: tabs, themes, modes, background tasks, STOP button, shortcuts.
import { api, uploadFiles } from "./api.js";
import { state } from "./state.js";
import { $, $$, clear, fmtBytes, fmtDur, h, initTooltips, modal, toast } from "./ui.js";
import capture from "./tabs/capture.js";
import mount from "./tabs/mount.js";
import library from "./tabs/library.js";
import analyze from "./tabs/analyze.js";
import stack from "./tabs/stack.js";
import solve from "./tabs/solve.js";
import process from "./tabs/process.js";
import exportTab from "./tabs/export.js";
import settingsTab from "./tabs/settings.js";
import { runWizard } from "./wizard.js";

const TABS = [capture, mount, library, analyze, stack, solve, process, exportTab, settingsTab];
let current = null;
const created = new Set();

async function boot() {
  initTooltips();
  const [status, settings] = await Promise.all([api.get("/api/status"), api.get("/api/settings")]);
  state.status = status; state.settings = settings; state.local = status.local;
  applyLook(settings);
  buildTabs();
  bindHeader();
  bindShortcuts();
  bindDrop();
  pollStatus();
  state.on("navigate", ({ tab, data }) => show(tab, data));
  state.on("project", (p) => {
    $("#stProject").textContent = p ? `Project: ${p.name}` : "No project open";
  });
  if (settings.last_project) {
    try { await state.openProject(settings.last_project); } catch { /* project moved or deleted */ }
  }
  const start = location.hash.slice(1) || (state.local ? (settings.imaging?.targets === "deepsky" ? "mount" : "capture") : "library");
  show(TABS.some((t) => t.id === start) ? start : "library");
  if (!settings.first_run_done && state.local) runWizard();
}

function applyLook(s) {
  const root = document.documentElement;
  root.dataset.theme = s.theme || "dark";
  root.dataset.mode = s.mode || "beginner";
  root.dataset.touch = s.touch_mode ? "1" : "0";
  root.dataset.nightImages = s.night_images === false ? "0" : "1";
  $$("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === root.dataset.mode));
  $$("#themeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.theme === root.dataset.theme));
}

function buildTabs() {
  const nav = $("#tabs"), main = $("#main");
  TABS.forEach((t, i) => {
    const disabled = t.localOnly && !state.local;
    const b = h("button", { role: "tab", dataset: { tab: t.id }, disabled,
      title: disabled ? "Camera and mount are controlled on the computer they are plugged into" : `${t.title} (Alt+${i + 1})`,
      onclick: () => show(t.id) }, t.icon ? h("span", {}, t.icon) : "", t.title, h("span", { class: "num" }, i + 1));
    nav.append(b);
    main.append(h("section", { class: "tab", id: `tab-${t.id}` }));
  });
}

function show(id, data) {
  const t = TABS.find((x) => x.id === id);
  if (!t || (t.localOnly && !state.local)) return;
  if (current && current !== t && current.hide) current.hide();
  $$(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${id}`));
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === id));
  const el = $(`#tab-${id}`);
  if (!created.has(id)) { created.add(id); t.create(el); }
  current = t;
  history.replaceState(null, "", `#${id}`);
  t.show && t.show(data);
}

function bindHeader() {
  $("#modeSeg").addEventListener("click", async (e) => {
    const m = e.target.dataset.mode; if (!m) return;
    document.documentElement.dataset.mode = m; applyLook({ ...state.settings, mode: m });
    await state.saveSettings({ mode: m });
    state.emit("mode", m);
  });
  $("#themeSeg").addEventListener("click", async (e) => {
    const t = e.target.dataset.theme; if (!t) return;
    applyLook({ ...state.settings, theme: t });
    await state.saveSettings({ theme: t });
    state.emit("theme", t);
  });
  $("#stopBtn").addEventListener("click", stopMount);
  $("#jobsBtn").addEventListener("click", () => { $("#jobsPop").classList.toggle("show"); renderJobs(); });
  document.addEventListener("click", (e) => { if (!e.target.closest("#jobsPop,#jobsBtn")) $("#jobsPop").classList.remove("show"); });
  $("#helpBtn").addEventListener("click", showHelp);
}

export async function stopMount() {
  try { await api.post("/api/mount/stop", {}, { quiet: true }); toast("Mount stopped", "warn"); }
  catch (e) { toast("STOP sent (the mount did not confirm: " + e.message + ")", "error"); }
}

let lastJobs = [];
async function pollStatus() {
  try {
    const s = await api.get("/api/status", { quiet: true });
    state.status = s;
    $("#stMem").textContent = `Memory ${fmtBytes(s.memory.process)}`;
    $("#stGpu").textContent = s.gpu.available && s.gpu.enabled ? `GPU: ${s.gpu.name}` : "CPU processing";
    $("#stWhere").textContent = s.local ? "This computer" : "Remote (camera/mount on the main computer)";
    const active = s.jobs || [];
    const badge = $("#jobsBadge");
    badge.hidden = !active.length; badge.textContent = active.length;
    if (active.length) {
      const j = active[0];
      $("#stMsg").textContent = `${j.title}: ${j.message || ""} ${Math.round(j.progress * 100)}%${j.eta_s ? " - about " + fmtDur(j.eta_s) + " left" : ""}`;
    } else if (lastJobs.length) $("#stMsg").textContent = "";
    lastJobs = active;
    if ($("#jobsPop").classList.contains("show")) renderJobs();
    if (state.local) {
      const m = await api.get("/api/mount/status", { quiet: true }).catch(() => null);
      $("#stopBtn").classList.toggle("show", !!(m && m.connected));
      state.emit("mount", m);
    }
  } catch { /* server restarting */ }
  setTimeout(pollStatus, 2000);
}

async function renderJobs() {
  const pop = $("#jobsPop");
  const list = await api.get("/api/jobs", { quiet: true }).catch(() => []);
  clear(pop);
  if (!list.length) { pop.append(h("div", { class: "empty" }, "No background tasks yet")); return; }
  for (const j of list.slice(0, 15)) {
    const running = j.status === "running" || j.status === "queued";
    pop.append(h("div", { class: "job" },
      h("div", { class: "row" }, h("b", { class: "grow" }, j.title),
        h("span", { class: `chip ${j.status === "done" ? "ok" : j.status === "error" ? "bad" : running ? "accent" : ""}` }, j.status),
        running ? h("button", { class: "small", onclick: async () => { await api.post(`/api/jobs/${j.id}/cancel`); renderJobs(); } }, "Cancel") : ""),
      running ? h("div", { class: "progress" }, h("div", { style: { width: `${Math.round(j.progress * 100)}%` } })) : "",
      h("div", { class: "tiny" }, j.error || j.message || "", j.eta_s ? ` - about ${fmtDur(j.eta_s)} left` : "", j.elapsed_s ? ` (${fmtDur(j.elapsed_s)})` : "")));
  }
}

function bindShortcuts() {
  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName || "");
    if (e.key === "Escape" && $("#stopBtn").classList.contains("show") && !document.querySelector(".modal-back")) { stopMount(); return; }
    if (e.altKey && /^[1-9]$/.test(e.key)) { const t = TABS[parseInt(e.key) - 1]; if (t) { show(t.id); e.preventDefault(); } return; }
    if (typing) return;
    if (e.key === "?") { showHelp(); return; }
    current && current.key && current.key(e);
  });
}

function bindDrop() {
  const hint = $("#dropHint");
  let depth = 0;
  window.addEventListener("dragenter", (e) => { if (e.dataTransfer?.types?.includes("Files")) { depth++; hint.classList.add("show"); } });
  window.addEventListener("dragleave", () => { depth = Math.max(0, depth - 1); if (!depth) hint.classList.remove("show"); });
  window.addEventListener("dragover", (e) => e.preventDefault());
  window.addEventListener("drop", async (e) => {
    e.preventDefault(); depth = 0; hint.classList.remove("show");
    const files = [...(e.dataTransfer?.files || [])];
    if (!files.length) return;
    const big = files.filter((f) => f.size > 2e9);
    if (big.length && state.local) {
      toast("Very large files are best added with Library > Add folder (no copy needed).", "warn");
    }
    toast(`Adding ${files.length} file(s)...`);
    try {
      const res = await uploadFiles(files, "dropped", (i, n, name) => { $("#stMsg").textContent = `Uploading ${i}/${n}: ${name}`; });
      toast(`Added ${res.length} file(s) to the Library (Uploads)`, "ok");
      state.selection = res.map((r) => r.path);
      state.emit("library-changed");
    } catch { /* toast already shown */ }
  });
}

function showHelp() {
  const keys = [["Alt+1 ... Alt+9", "Switch tabs"], ["Esc", "STOP the mount (when connected)"], ["R", "Capture: start/stop recording"],
    ["S", "Capture: snapshot"], ["Ctrl+Z / Ctrl+Y", "Post-process: undo / redo"], ["B", "Post-process: before/after split"],
    ["F / 0", "Image: fit to window / 100%"], ["?", "This help"]];
  modal({
    title: "Help", wide: true,
    body: h("div", {},
      h("h3", {}, "The workflow"),
      h("ol", {},
        h("li", {}, h("b", {}, "Capture"), " - connect the camera, frame and focus, then record a video (planets/Moon) or a series of exposures (deep sky)."),
        h("li", {}, h("b", {}, "Analyze"), " - every frame is scored; keep the best ones."),
        h("li", {}, h("b", {}, "Stack"), " - the kept frames are aligned and combined into one clean image."),
        h("li", {}, h("b", {}, "Post-process"), " - sharpen, stretch, colour and clean the image. Every step can be changed later."),
        h("li", {}, h("b", {}, "Export"), " - save a finished picture in any format and size.")),
      h("p", {}, "In Beginner mode each tab shows a blue hint box telling you what to do next. Hover over dotted words for an explanation."),
      h("h3", {}, "Keyboard shortcuts"),
      h("div", { class: "kv" }, ...keys.flatMap(([k, v]) => [h("span", { class: "kbd" }, k), h("span", {}, v)])),
      h("h3", { style: { marginTop: "12px" } }, "Problems?"),
      h("p", {}, "Settings > Support lets you save a bug-report file (logs and settings) to attach to an email. The README file explains installation, every tab and troubleshooting.")),
    buttons: [{ label: "Close", class: "primary" }],
  });
}

boot().catch((e) => { console.error(e); document.body.append(h("div", { class: "dangerbox", style: { margin: "20px" } }, "AstroFlow could not start: " + e.message)); });
