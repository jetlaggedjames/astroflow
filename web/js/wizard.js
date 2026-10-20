// First-run setup wizard (can be run again from Settings). Also exports the building blocks Settings uses.
import { api, waitJob } from "./api.js";
import { state } from "./state.js";
import { clear, debounce, fmtBytes, fmtNum, h, modal, pickPath, toast } from "./ui.js";

// ------------------------------------------------------------- building blocks
export function equipmentForm(eq, onChange) {
  const box = h("div");
  const inp = (key, label, type = "text", attrs = {}) => {
    const i = h("input", { type, value: eq[key] ?? "", style: { width: "100%" }, ...attrs });
    i.addEventListener("change", () => { eq[key] = type === "number" ? parseFloat(i.value) : i.value; onChange(); });
    return h("div", { class: "field" }, h("label", {}, label), i);
  };
  const barlows = h("div");
  const paintB = () => {
    clear(barlows);
    (eq.barlows || []).forEach((b, i) => {
      const n = h("input", { value: b.name, style: { flex: 1 } }); n.addEventListener("change", () => { b.name = n.value; onChange(); });
      const f = h("input", { type: "number", step: 0.1, min: 0.3, max: 6, value: b.factor, style: { width: "80px" } }); f.addEventListener("change", () => { b.factor = parseFloat(f.value); onChange(); });
      barlows.append(h("div", { class: "row", style: { marginBottom: "4px" } },
        h("input", { type: "radio", name: "activeBarlow", checked: (eq.active_barlow || 0) === i, title: "In use now", onchange: () => { eq.active_barlow = i; onChange(); } }),
        n, f, h("span", { class: "tiny" }, "x"), i > 0 ? h("button", { class: "small ghost", onclick: () => { eq.barlows.splice(i, 1); if (eq.active_barlow >= eq.barlows.length) eq.active_barlow = 0; paintB(); onChange(); } }, "✕") : ""));
    });
    barlows.append(h("button", { class: "small", onclick: () => { eq.barlows.push({ name: "New Barlow / reducer", factor: 2 }); paintB(); onChange(); } }, "+ Add Barlow / reducer"));
  };
  paintB();
  box.append(
    h("div", { class: "grid2" }, inp("telescope", "Telescope"), inp("aperture_mm", "Aperture (mm)", "number")),
    h("div", { class: "grid2" }, inp("focal_length_mm", "Focal length (mm)", "number"), h("div", { class: "field" }, h("label", {}, "Mount type"),
      (() => { const s = h("select", { onchange: (e) => { eq.mount_type = e.target.value; onChange(); } }, h("option", { value: "altaz", selected: eq.mount_type !== "eq" }, "Alt-az (Dobsonian GoTo)"), h("option", { value: "eq", selected: eq.mount_type === "eq" }, "Equatorial / on a wedge")); return s; })())),
    h("div", { class: "field" }, h("label", {}, "Barlows and reducers (the dot marks the one in use)"), barlows),
    h("div", { class: "grid2" }, inp("camera", "Camera"), inp("pixel_um", "Pixel size (µm)", "number", { step: 0.01 })),
    h("div", { class: "grid2" }, inp("sensor_w_px", "Sensor width (px)", "number"), inp("sensor_h_px", "Sensor height (px)", "number")),
    inp("filter", "Filter"));
  return box;
}

export async function fovBox(eq) {
  const b = (eq.barlows || [])[eq.active_barlow || 0]?.factor || 1;
  const r = await api.get("/api/wizard/fov?" + api.q({ focal_mm: eq.focal_length_mm, pixel_um: eq.pixel_um, width_px: eq.sensor_w_px, height_px: eq.sensor_h_px, barlow: b }), { quiet: true });
  return { r, el: h("div", { class: "okbox tiny" }, `With this setup: ${fmtNum(r.scale_arcsec, 2)} arc-seconds per pixel, field of view ${r.fov_arcmin[0]}' x ${r.fov_arcmin[1]}' (${r.fov_deg[0]}° x ${r.fov_deg[1]}°).` +
    ` Jupiter (about 45") would be ${Math.round(45 / r.scale_arcsec)} pixels wide.`) };
}

export function locationForm(loc, onChange) {
  const box = h("div");
  const results = h("div", { class: "listbox", style: { maxHeight: "180px" } });
  const search = h("input", { placeholder: "Type your town or city, e.g. 'Leeds' or 'Springfield, IL'", style: { width: "100%" } });
  const lat = h("input", { type: "number", step: 0.0001, value: loc.lat ?? "" }), lon = h("input", { type: "number", step: 0.0001, value: loc.lon ?? "" });
  const elev = h("input", { type: "number", value: loc.elevation_m ?? 0 });
  const tz = h("input", { value: loc.tz || "UTC", list: "tzlist" });
  const name = h("div", { class: "tiny" }, loc.name ? `Saved: ${loc.name}` : "");
  const tzlist = h("datalist", { id: "tzlist" });
  api.get("/api/wizard/timezones", { quiet: true }).then((l) => l.forEach((t) => tzlist.append(h("option", { value: t })))).catch(() => {});
  const upd = () => { loc.lat = lat.value === "" ? null : parseFloat(lat.value); loc.lon = lon.value === "" ? null : parseFloat(lon.value); loc.elevation_m = parseFloat(elev.value || 0); loc.tz = tz.value || "UTC"; onChange(); };
  [lat, lon, elev, tz].forEach((i) => i.addEventListener("change", upd));
  search.addEventListener("input", debounce(async () => {
    clear(results);
    if (search.value.trim().length < 2) return;
    const list = await api.get("/api/wizard/cities?" + api.q({ q: search.value }), { quiet: true }).catch(() => []);
    for (const c of list) results.append(h("div", { class: "item", onclick: () => {
      lat.value = c.lat.toFixed(4); lon.value = c.lon.toFixed(4); if (c.tz) tz.value = c.tz; loc.name = c.label; name.textContent = `Chosen: ${c.label}`; clear(results); upd();
    } }, h("span", { style: { flex: 1 } }, c.label), h("small", {}, `${c.lat.toFixed(2)}, ${c.lon.toFixed(2)}`)));
    if (!list.length) results.append(h("div", { class: "item tiny" }, "No match - you can type latitude/longitude below instead."));
  }, 200));
  box.append(h("div", { class: "field" }, h("label", {}, "Find your place (works offline)"), search), results, name,
    h("div", { class: "grid3" }, h("div", { class: "field" }, h("label", {}, "Latitude (north +)"), lat), h("div", { class: "field" }, h("label", {}, "Longitude (east +)"), lon), h("div", { class: "field" }, h("label", {}, "Elevation (m)"), elev)),
    h("div", { class: "field" }, h("label", {}, "Time zone"), tz), tzlist,
    h("div", { class: "tiny" }, "Used for what's up tonight, the mount's sky model and field-rotation advice. A city-level position is plenty."));
  return box;
}

export function folderForm(folders, onChange) {
  const box = h("div");
  for (const [k, label] of [["captures", "Captures (raw recordings - needs lots of space)"], ["projects", "Projects (stacks and edits)"], ["exports", "Exports (finished pictures)"]]) {
    const pathEl = h("span", { class: "mono tiny", style: { flex: 1, wordBreak: "break-all" } }, folders[k]);
    const free = h("span", { class: "tiny" });
    const check = async () => { try { const r = await api.get("/api/fs/space?" + api.q({ path: folders[k] }), { quiet: true }); free.textContent = `${fmtBytes(r.free)} free`; free.className = "tiny chip " + (r.free < 50e9 ? "warn" : "ok"); } catch { free.textContent = "(will be created)"; } };
    check();
    box.append(h("div", { class: "field" }, h("label", {}, label), h("div", { class: "row" }, pathEl, free,
      h("button", { class: "small", onclick: async () => { const p = await pickPath({ title: label, dirs: true, start: folders[k] }); if (p) { folders[k] = p; pathEl.textContent = p; check(); onChange(); } } }, "Change..."))));
  }
  box.append(h("div", { class: "tiny" }, "Planetary videos are big (a 2-minute Jupiter video at full speed can be 10-20 GB). Put Captures on your largest, fastest drive (an SSD is best for recording)."));
  return box;
}

export async function solverPanel() {
  const box = h("div");
  const paint = async () => {
    clear(box);
    let st, fov;
    try { [st, fov] = await Promise.all([api.get("/api/wizard/solver", { quiet: true }), api.get("/api/wizard/fov", { quiet: true })]); } catch { box.append(h("div", { class: "tiny" }, "Could not read the solver status.")); return; }
    const rec = fov.recommended;
    box.append(h("p", {}, fov.explain),
      h("div", { class: "kv" }, h("span", {}, "Your field of view"), h("span", {}, `${fov.fov_arcmin[0]}' x ${fov.fov_arcmin[1]}' (${fov.fov_deg[0]}° x ${fov.fov_deg[1]}°)`),
        h("span", {}, "Solver program (ASTAP)"), h("span", {}, st.astap ? h("span", { class: "chip ok" }, "installed") : h("span", { class: "chip warn" }, "not installed (about 3 MB)")),
        h("span", {}, "Star database"), h("span", {}, st.databases?.length ? h("span", { class: "chip ok" }, st.databases.join(", ").toUpperCase()) : h("span", { class: "chip warn" }, "none"))),
      h("div", { class: rec?.name ? "okbox tiny" : "warnbox tiny" }, rec?.text || "No database suits this field."));
    const jobBox = h("div");
    const install = async (cli, db) => {
      try {
        const { job } = await api.post("/api/wizard/install_astap", { cli, database: db });
        const bar = h("div", { class: "tiny" }); clear(jobBox); jobBox.append(bar);
        const r = await waitJob(job, (j) => { bar.textContent = j.message || j.status; });
        toast(r.note || "Installed", "ok"); paint();
      } catch (e) { toast(e.message, "error"); clear(jobBox); }
    };
    const dbs = Object.entries(fov.databases).filter(([k, v]) => v.min_fov <= Math.min(...fov.fov_deg) && Math.min(...fov.fov_deg) <= v.max_fov);
    if (state.local) box.append(h("div", { class: "row", style: { marginTop: "8px" } },
      !st.astap ? h("button", { class: "primary", onclick: () => install(true, rec?.name || null) }, `Download ASTAP${rec?.name ? ` + ${rec.name.toUpperCase()} (about ${rec.size_mb} MB)` : ""}`) : "",
      st.astap ? h("select", { id: "dbPick" }, ...dbs.map(([k, v]) => h("option", { value: k, selected: k === rec?.name }, `${k.toUpperCase()} - about ${v.size_mb} MB`))) : "",
      st.astap ? h("button", { onclick: () => install(false, document.getElementById("dbPick").value) }, "Download database") : ""),
    h("div", { class: "tiny" }, "Downloads come from the ASTAP project on SourceForge (free, open source, by Han Kleijn). Internet is needed once; afterwards solving is fully offline."),
    jobBox);
    const s = state.settings.solver;
    const allow = h("input", { type: "checkbox", checked: !!s.allow_online, onchange: () => state.saveSettings({ solver: { allow_online: allow.checked } }) });
    const key = h("input", { type: "password", value: s.astrometry_api_key || "", placeholder: "API key from nova.astrometry.net (free account)", style: { width: "100%" } });
    key.addEventListener("change", () => state.saveSettings({ solver: { astrometry_api_key: key.value } }));
    box.append(h("hr"), h("label", { class: "check" }, allow, "Allow the online fallback (Astrometry.net) when offline solving fails"),
      h("div", { class: "tiny" }, "Off by default. When on, only the image being solved is sent (as a private upload)."), state.local ? h("div", { class: "field" }, h("label", {}, "Astrometry.net API key"), key) : "");
  };
  await paint();
  return box;
}

export async function cameraPanel() {
  const box = h("div");
  const paint = async () => {
    clear(box);
    const r = await api.get("/api/wizard/playerone", { quiet: true }).catch(() => null);
    if (!r) return;
    box.append(h("p", { class: "tiny" }, r.help));
    if (r.found?.length) box.append(h("div", { class: "okbox tiny" }, `Player One library found: ${r.found[0]}${r.sdk_version ? ` (SDK ${r.sdk_version})` : ""}`,
      r.cameras ? h("div", {}, r.cameras.length ? `Camera(s) connected: ${r.cameras.map((c) => c.name).join(", ")}` : "No camera plugged in right now (that's fine).") : "",
      r.error ? h("div", {}, r.error) : ""));
    else box.append(h("div", { class: "warnbox tiny" }, "Player One library not found yet."));
    const file = h("input", { type: "file", accept: ".zip", style: { display: "none" }, onchange: async (e) => {
      const f = e.target.files[0]; if (!f) return;
      const fd = new FormData(); fd.append("file", f);
      try { const res = await api.post("/api/wizard/playerone/upload", fd); toast(`Installed ${res.installed}`, "ok"); paint(); } catch { /* toast */ }
    } });
    if (state.local) box.append(h("div", { class: "row" }, h("button", { class: "small", onclick: () => file.click() }, "Give AstroFlow the Player One SDK zip..."), file,
      h("button", { class: "small ghost", onclick: paint }, "Check again")));
    box.append(h("div", { class: "tiny", style: { marginTop: "6px" } }, "No camera yet? Capture works with a built-in simulator so you can practise the whole workflow."));
  };
  await paint();
  return box;
}

// ------------------------------------------------------------------ wizard
const MOUNT_CHOICES = [
  ["synscan_app", "SynScan app + this program (most reliable)", "Run Sky-Watcher's free SynScan Pro app on this PC (or phone), connect it to the mount's WiFi, and let AstroFlow talk to the app. The app handles alignment; AstroFlow sends GoTo/sync. Choose 'ASCOM driver' instead if you installed the SynScan ASCOM driver."],
  ["synscan_hc", "USB cable to the hand controller", "Very reliable and beginner friendly: a USB cable from the PC to the SynScan hand controller. The hand controller keeps your star alignment."],
  ["synscan_wifi", "Direct WiFi to the mount (no extra software)", "AstroFlow speaks the SynScan motor protocol over the mount's WiFi itself. Simplest setup, but untested on real hardware - try it with a hand on STOP."],
  ["ascom", "ASCOM driver (Windows)", "For the SynScan App driver or any other ASCOM telescope driver."],
  ["simulator", "Simulator (practice)", "A pretend mount - good for learning the app indoors."],
  ["none", "No mount / decide later", "You can connect a mount any time in the Mount tab."],
];

export async function runWizard() {
  const s = JSON.parse(JSON.stringify(state.settings));
  const w = { equipment: s.equipment, imaging: s.imaging, location: s.location, folders: s.folders, mount: { driver: s.mount.driver }, mode: s.mode || "beginner", theme: s.theme || "dark" };
  const steps = ["Welcome", "Telescope & camera", "What you photograph", "Camera driver", "Mount", "Plate solving", "Location", "Folders", "Finish"];
  let i = 0;
  const body = h("div", { class: "wizard" });
  const m = modal({ title: "Set up AstroFlow", body, wide: true, buttons: [] });
  const footer = m.el.querySelector("footer");
  const draw = async () => {
    clear(body); clear(footer);
    body.append(h("div", { class: "steps" }, ...steps.map((t, k) => h("span", { class: k === i ? "on" : k < i ? "done" : "" }, `${k + 1}. ${t}`))));
    const page = h("div");
    body.append(page);
    await PAGES[i](page, w);
    footer.append(
      h("button", { class: "ghost", onclick: async () => { m.close(); await state.saveSettings({ first_run_done: true }).catch(() => {}); toast("You can run the setup again from Settings > Support", "info"); } }, "Skip setup"),
      h("span", { class: "spacer" }),
      i > 0 ? h("button", { onclick: () => { i--; draw(); } }, "Back") : "",
      i < steps.length - 1 ? h("button", { class: "primary", onclick: () => { i++; draw(); } }, i === 0 ? "Start" : "Next") :
        h("button", { class: "primary", onclick: async () => {
          try {
            const res = await api.post("/api/wizard/finish", { equipment: w.equipment, imaging: w.imaging, location: w.location, folders: w.folders, mount: w.mount, mode: w.mode, theme: w.theme });
            state.settings = res; state.emit("settings", res);
            document.documentElement.dataset.mode = w.mode; document.documentElement.dataset.theme = w.theme;
            document.querySelectorAll("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === w.mode));
            document.querySelectorAll("#themeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.theme === w.theme));
            state.emit("mode", w.mode);
            m.close(); toast("All set - clear skies!", "ok");
          } catch { /* toast */ }
        } }, "Finish"));
  };
  const PAGES = [
    (p) => p.append(h("h2", {}, "Welcome to AstroFlow"),
      h("p", {}, "AstroFlow takes you from the telescope to a finished picture: ", h("b", {}, "Capture → Analyze → Stack → Post-process → Export"), ", with your files organised in the Library."),
      h("p", {}, "This short setup tells AstroFlow about your equipment and where you observe from. Every answer can be changed later in Settings, and you can skip any step."),
      h("p", { class: "tiny" }, "Nothing is uploaded anywhere. Downloads (star database, GPU package) only happen when you press their button.")),
    async (p, w) => {
      const fov = h("div");
      const upd = async () => { clear(fov); try { fov.append((await fovBox(w.equipment)).el); } catch { /* */ } };
      p.append(h("h3", {}, "Your telescope and camera"), h("p", { class: "tiny" }, "Pre-filled for a Sky-Watcher Flextube 300P with a Player One Uranus-C. Change anything that differs."),
        equipmentForm(w.equipment, upd), fov);
      upd();
    },
    (p, w) => {
      const opt = (v, title, text) => h("label", { class: "card", style: { display: "block", marginBottom: "8px", cursor: "pointer" } },
        h("div", { class: "row" }, h("input", { type: "radio", name: "tgt", checked: w.imaging.targets === v, onchange: () => { w.imaging.targets = v; } }), h("b", {}, title)), h("div", { class: "tiny" }, text));
      p.append(h("h3", {}, "What do you mostly photograph?"),
        opt("planetary", "Planets and the Moon", "Short-exposure videos, lucky imaging and wavelet sharpening are front and centre. Capture opens first."),
        opt("deepsky", "Deep sky (galaxies, nebulae, clusters)", "Longer exposures, live stacking, calibration frames and plate solving are front and centre. The Mount tab opens first."),
        opt("both", "Both", "Everything, with planetary defaults where it matters."),
        h("div", { class: "okbox tiny" }, h("b", {}, "Object catalogues are already included and work offline: "),
          "Messier, Caldwell, NGC and IC (13,372 galaxies, nebulae and clusters with common names), 83,479 stars to magnitude 9 with 431 star names, ",
          "plus the planets and the Moon computed for any date. Nothing to download."));
    },
    async (p) => { p.append(h("h3", {}, "Camera driver"), await cameraPanel()); },
    (p, w) => {
      p.append(h("h3", {}, "How will you connect the mount?"),
        h("p", { class: "tiny" }, "Ranked by reliability and ease for a SynScan GoTo Dobsonian. If your PC joins the mount's WiFi, that WiFi has no internet - a second WiFi adapter or an Ethernet cable keeps you online."),
        ...MOUNT_CHOICES.map(([v, title, text]) => h("label", { class: "card", style: { display: "block", marginBottom: "6px", cursor: "pointer" } },
          h("div", { class: "row" }, h("input", { type: "radio", name: "mnt", checked: w.mount.driver === v, onchange: () => { w.mount.driver = v; } }), h("b", {}, title)), h("div", { class: "tiny" }, text))));
    },
    async (p) => { p.append(h("h3", {}, "Plate solving (finding where the telescope points)"), await solverPanel(), h("p", { class: "tiny" }, "You can skip this and set it up later in Settings > Plate solving.")); },
    (p, w) => { p.append(h("h3", {}, "Where do you observe from?"), locationForm(w.location, () => {})); },
    (p, w) => { p.append(h("h3", {}, "Where should files go?"), folderForm(w.folders, () => {})); },
    (p, w) => {
      const seg = (key, opts) => h("div", { class: "seg" }, ...opts.map(([v, l]) => { const b = h("button", { class: w[key] === v ? "on" : "", onclick: () => { w[key] = v; b.parentElement.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); } }, l); return b; }));
      p.append(h("h3", {}, "Almost done"),
        h("div", { class: "field" }, h("label", {}, "How much do you want to see?"), seg("mode", [["beginner", "Beginner - fewer controls, hints"], ["advanced", "Advanced - every setting"]])),
        h("p", { class: "tiny" }, "Switch any time with the Beginner/Advanced buttons at the top. Switching never loses work."),
        h("div", { class: "field" }, h("label", {}, "Display"), seg("theme", [["dark", "Dark"], ["night", "Night (red, keeps night vision)"], ["day", "Day (high contrast)"]])),
        h("p", {}, "Press ", h("b", {}, "Finish"), " to save. Tip: the Help button (?) lists keyboard shortcuts; the red STOP button appears whenever a mount is connected."));
    },
  ];
  draw();
}
