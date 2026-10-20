// Mount: connect, where it points, GoTo with safety confirmation, hold-to-move d-pad, tracking, tonight's targets.
import { api } from "../api.js";
import { drawAltitude } from "../charts.js";
import { hint, runJob } from "../common.js";
import { go, state } from "../state.js";
import { clear, confirmDialog, debounce, fmtNum, h, modal, section, term, toast } from "../ui.js";

const SPEEDS = ["1x (slowest)", "2x", "8x", "16x", "32x", "64x", "128x", "400x", "800x (fastest)"];
const INTERESTS = [["planet", "Planets"], ["moon", "Moon"], ["galaxy", "Galaxies"], ["nebula", "Nebulae"], ["cluster", "Clusters"]];
const DRIVER_NOTES = {
  synscan_wifi: "Join this PC to the SynScan WiFi network (SynScan_xxxx). This PC then has no internet while connected unless it also has Ethernet or a second WiFi adapter. Close the SynScan phone app first - only one program can talk to the mount.",
  synscan_serial: "USB cable from this PC to the hand controller (set it to PC-Direct mode) or directly to the motor board.",
  synscan_hc: "USB cable to the SynScan hand controller (it keeps its own star alignment).",
  synscan_app: "The SynScan app (Windows or phone) is connected to the mount; AstroFlow talks to the app over the network.",
  ascom: "Uses an ASCOM driver, e.g. the 'SynScan App driver' from Sky-Watcher. Install ASCOM Platform + the driver first.",
  alpaca: "ASCOM Alpaca over the network (e.g. the SynScan app's Alpaca server, or ASCOM Remote).",
  simulator: "A pretend mount for practice. The simulated camera's deep-sky view follows it.",
};

let el, ui = {}, st = null, moveTimer = null, moveDir = null, speed = 6, picked = null, tonightData = null;

export default {
  id: "mount", title: "Mount", localOnly: true,
  create(root) { el = root; build(); state.on("mount", (m) => { st = m; paint(); }); },
  show() { loadTonight(); },
  hide() { stopMove(); },
  key(e) {
    const dir = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" }[e.key];
    if (dir && st?.connected) { e.preventDefault(); if (moveDir !== dir) startMove(dir); }
  },
};

document.addEventListener("keyup", (e) => { if (moveDir && e.key.startsWith("Arrow")) stopMove(); });
window.addEventListener("blur", () => { if (moveDir) stopMove(); });

function build() {
  ui.side = h("div", { class: "side" });
  ui.center = h("div", { class: "center scroll pad" });
  el.append(h("div", { class: "split" }, ui.side, ui.center));
  ui.conn = h("div", { class: "panel" });
  ui.pos = h("div", { class: "panel" });
  ui.pad = h("div", { class: "panel" });
  ui.track = h("div");
  ui.log = h("div");
  ui.side.append(
    hint("Connect the mount, then pick a target on the right and press ", h("b", {}, "GoTo"),
      ". The big red ", h("b", {}, "STOP"), " button (or Esc) halts the telescope at any time."),
    ui.conn, ui.pos, ui.pad,
    section("Tracking, park and alignment", ui.track, { open: false }),
    section("Command log", ui.log, { open: false, adv: true }));
  ui.target = h("div", { class: "panel" });
  ui.search = h("div", { class: "panel" });
  ui.tonight = h("div", { class: "panel" });
  ui.center.append(ui.target, ui.search, ui.tonight);
  renderConn(); renderPad(); renderTrack(); renderSearch(); renderTarget();
}

// ----------------------------------------------------------------- connect
async function renderConn() {
  const box = ui.conn; clear(box);
  const s = state.settings?.mount || {};
  box.append(h("h3", {}, "Mount"));
  if (st?.connected) {
    box.append(h("div", { class: "row" }, h("span", { class: "chip ok" }, "Connected"), h("b", {}, st.model || st.driver)),
      st.untested_on_hardware ? h("div", { class: "warnbox tiny" }, "This connection method is untested on real hardware. Keep a hand near STOP the first time and check that directions are correct.") : "",
      h("button", { style: { marginTop: "6px" }, onclick: () => api.post("/api/mount/disconnect") }, "Disconnect"));
    return;
  }
  const drivers = st?.drivers || { none: "No mount", simulator: "Simulator", synscan_wifi: "SynScan WiFi" };
  const sel = h("select", { style: { width: "100%" } }, ...Object.entries(drivers).filter(([k]) => k !== "none")
    .map(([k, v]) => h("option", { value: k, selected: k === (s.driver !== "none" ? s.driver : "synscan_wifi") }, v)));
  const extra = h("div");
  const note = h("div", { class: "tiny" });
  const fields = async () => {
    clear(extra); note.textContent = DRIVER_NOTES[sel.value] || "";
    const d = sel.value;
    const inp = (key, label, val, type = "text") => {
      const i = h("input", { value: val ?? "", type, style: { width: "100%" } });
      i.addEventListener("change", () => state.saveSettings({ mount: { [key]: type === "number" ? Number(i.value) : i.value } }));
      return h("div", { class: "field" }, h("label", {}, label), i);
    };
    if (d === "synscan_wifi" || d === "synscan_app") extra.append(h("div", { class: "grid2" }, inp("host", "Address", s.host || "192.168.4.1"), inp("port", "Port", d === "synscan_app" ? 11882 : (s.port || 11880), "number")));
    if (d === "synscan_serial" || d === "synscan_hc") {
      const ports = await api.get("/api/mount/serial_ports", { quiet: true }).catch(() => []);
      const ps = h("select", { style: { width: "100%" }, onchange: (e) => state.saveSettings({ mount: { serial_port: e.target.value } }) },
        h("option", { value: "" }, ports.length ? "Choose..." : "No serial ports found"), ...ports.map((p) => h("option", { value: p.device, selected: p.device === s.serial_port }, `${p.device} - ${p.description}`)));
      extra.append(h("div", { class: "field" }, h("label", {}, "Serial (COM) port"), ps));
    }
    if (d === "ascom") extra.append(h("div", { class: "row" }, h("span", { class: "tiny mono" }, s.ascom_progid || "No driver chosen"),
      h("button", { class: "small", onclick: async () => { const r = await api.post("/api/mount/ascom/choose", { device: "Telescope" }); if (r.progid) { await state.saveSettings({}); renderConn(); } } }, "Choose driver...")));
    if (d === "alpaca") extra.append(inp("alpaca_url", "Alpaca server", s.alpaca_url), inp("alpaca_device", "Device number", s.alpaca_device, "number"),
      h("button", { class: "small", onclick: async () => { const r = await api.get("/api/mount/alpaca/discover"); toast(r.length ? `Found: ${r.map((x) => x.url || x).join(", ")}` : "No Alpaca servers found", r.length ? "ok" : "warn"); } }, "Search network"));
  };
  sel.addEventListener("change", fields); fields();
  box.append(h("div", { class: "field" }, h("label", {}, "How is it connected?"), sel), note, extra,
    h("button", { class: "primary big", style: { marginTop: "6px", width: "100%" }, onclick: async () => {
      try { await api.post("/api/mount/connect", { driver: sel.value }); toast("Mount connected", "ok"); } catch { /* toast */ }
    } }, "Connect"),
    h("div", { class: "tiny", style: { marginTop: "6px" } }, "AstroFlow never moves the telescope unless you press a button. Large or unusual moves always ask first."));
}

// ---------------------------------------------------------------- position
function paint() {
  if (!ui.pos) return;
  if (st && !!st.connected !== ui._was) { ui._was = !!st.connected; renderConn(); renderTrack(); }
  const box = ui.pos; clear(box);
  box.append(h("h3", {}, "Pointing"));
  if (!st?.connected) { box.append(h("div", { class: "tiny" }, "Not connected.")); paintLog(); return; }
  box.append(h("div", { class: "kv" },
    h("span", {}, term("RA")), h("b", {}, st.ra !== null ? fmtRa(st.ra) : "-"),
    h("span", {}, term("Dec")), h("b", {}, st.dec !== null ? fmtDec(st.dec) : "-"),
    h("span", {}, term("Altitude")), h("b", {}, st.alt !== null ? `${fmtNum(st.alt, 1)}°` : "-"),
    h("span", {}, term("Azimuth")), h("b", {}, st.az !== null ? `${fmtNum(st.az, 1)}° ${compass(st.az)}` : "-")),
  h("div", { class: "row", style: { marginTop: "6px" } },
    st.slewing ? h("span", { class: "chip warn" }, "Slewing...") : "",
    st.moving ? h("span", { class: "chip warn" }, "Moving") : "",
    st.tracking ? h("span", { class: "chip ok" }, `Tracking (${st.track_rate})`) : h("span", { class: "chip" }, "Not tracking"),
    st.parked ? h("span", { class: "chip" }, "Parked") : "",
    h("span", { class: "chip " + (st.alignment_points ? "ok" : "warn"), "data-tip": "Each Sync on a known star/object improves GoTo accuracy. 2-3 syncs spread across the sky work best." },
      `${st.alignment_points || 0} alignment point(s)`)),
  st.target ? h("div", { class: "tiny" }, `Target: ${st.target.label || ""}`) : "",
  st.error ? h("div", { class: "dangerbox tiny" }, st.error) : "", st.message ? h("div", { class: "tiny" }, st.message) : "",
  ...(st.warnings || []).map((w) => h("div", { class: "warnbox tiny" }, w)),
  h("div", { class: "tiny" }, `Altitude limit ${fmtNum(st.alt_limit, 0)}° (Settings > Mount)`));
  paintLog();
}

function paintLog() {
  const box = ui.log; clear(box);
  const list = (st?.log || []).slice().reverse();
  if (!list.length) { box.append(h("div", { class: "tiny" }, "No commands yet.")); return; }
  box.append(h("div", { class: "listbox", style: { maxHeight: "200px" } }, ...list.map((l) =>
    h("div", { class: "item tiny" }, h("span", { class: "mono" }, l.time || ""), h("b", {}, l.action), h("span", { style: { flex: 1 } }, l.detail || ""), l.source && l.source !== "user" ? h("span", { class: "chip warn" }, l.source) : ""))));
}

// ------------------------------------------------------------------- d-pad
function renderPad() {
  const box = ui.pad; clear(box);
  const btn = (dir, label, tip) => {
    const b = h("button", { title: tip, "aria-label": tip }, label);
    b.addEventListener("pointerdown", (e) => { e.preventDefault(); b.setPointerCapture(e.pointerId); startMove(dir); });
    for (const ev of ["pointerup", "pointercancel", "lostpointercapture"]) b.addEventListener(ev, stopMove);
    b.addEventListener("contextmenu", (e) => e.preventDefault());
    return b;
  };
  const sp = h("select", { style: { width: "100%" }, onchange: (e) => { speed = parseInt(e.target.value); } }, ...SPEEDS.map((s, i) => h("option", { value: i, selected: i === speed }, s)));
  box.append(h("h3", {}, "Move by hand"),
    h("div", { class: "tiny" }, "Hold a button (or an arrow key) to move; release to stop. If the connection drops, the mount stops by itself within 1.5 s."),
    h("div", { class: "dpad", style: { margin: "8px 0" } },
      h("span"), btn("up", "▲", "Up"), h("span"),
      btn("left", "◀", "Left"), h("button", { class: "mid danger", onclick: () => api.post("/api/mount/stop") }, "STOP"), btn("right", "▶", "Right"),
      h("span"), btn("down", "▼", "Down"), h("span")),
    h("div", { class: "field" }, h("label", {}, "Speed (multiples of the sky's motion)"), sp));
}

function startMove(dir) {
  if (!st?.connected) { toast("Connect the mount first", "warn"); return; }
  moveDir = dir;
  const send = () => api.post("/api/mount/move", { direction: dir, rate: speed }, { quiet: true })
    .then((r) => { if (r && r.moving === false && r.message) { toast(r.message, "warn"); stopMove(); } }).catch(() => stopMove());
  send();
  clearInterval(moveTimer);
  moveTimer = setInterval(send, 400);     // heartbeat: the server stops the move if these stop arriving
}
function stopMove() {
  if (!moveDir && !moveTimer) return;
  clearInterval(moveTimer); moveTimer = null; moveDir = null;
  api.post("/api/mount/move/stop", {}, { quiet: true }).catch(() => api.post("/api/mount/stop", {}, { quiet: true }));
}

// ---------------------------------------------------------------- tracking
function renderTrack() {
  const box = ui.track; clear(box);
  const rate = h("select", {}, h("option", { value: "sidereal" }, "Stars (sidereal)"), h("option", { value: "lunar" }, "Moon (lunar)"), h("option", { value: "solar" }, "Sun rate (planets ok too)"));
  box.append(
    h("div", { class: "row" }, rate,
      h("button", { onclick: () => api.post("/api/mount/tracking", { on: true, rate: rate.value }) }, "Track"),
      h("button", { onclick: () => api.post("/api/mount/tracking", { on: false }) }, "Stop tracking")),
    h("div", { class: "row", style: { marginTop: "8px" } },
      h("button", { onclick: park }, "Park"),
      h("button", { "data-tip": "Tell the mount it is level and pointing due north right now (the SynScan start position).", onclick: async () => {
        if (await confirmDialog("Set home position", "Only do this when the tube is level and pointing due north. It clears the alignment.", { ok: "Yes, it is level and north" })) api.post("/api/mount/home");
      } }, "Set home here"),
      h("button", { onclick: async () => { if (await confirmDialog("Reset alignment", "Forget all sync points? GoTo accuracy will drop until you sync again.", { ok: "Reset", danger: true })) api.post("/api/mount/alignment/reset"); } }, "Reset alignment")));
}

async function park() {
  const r = await api.post("/api/mount/park", {});
  if (r.needs_confirmation && await confirmDialog("Park the telescope?", r.message, { ok: "Park" })) await api.post("/api/mount/park", { confirm: r.token });
}

// ----------------------------------------------------------------- targets
function renderSearch() {
  const box = ui.search; clear(box);
  const results = h("div", { class: "listbox", style: { maxHeight: "220px" } });
  const inp = h("input", { placeholder: "Search: M13, Orion Nebula, NGC 7000, Jupiter, Vega...", style: { flex: 1 } });
  const run = debounce(async () => {
    clear(results);
    const q = inp.value.trim(); if (q.length < 2) return;
    const list = await api.get("/api/catalog/search?" + api.q({ q, limit: 15 }), { quiet: true }).catch(() => []);
    for (const o of list) results.append(h("div", { class: "item", onclick: () => pick(o.id || o.name) }, h("b", {}, o.label || o.name), h("span", { class: "tiny", style: { flex: 1 } }, `${o.kind || ""}${o.mag !== null && o.mag !== undefined ? ", mag " + o.mag : ""}${o.const ? ", " + o.const : ""}`)));
    if (!list.length) results.append(h("div", { class: "item tiny" }, "Nothing found"));
  }, 200);
  inp.addEventListener("input", run);
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") pick(inp.value.trim()); });
  box.append(h("h3", {}, "Find a target"), h("div", { class: "row" }, inp,
    ...["Moon", "Jupiter", "Saturn", "Mars"].map((n) => h("button", { class: "small", onclick: () => pick(n) }, n))), results);
}

async function loadTonight() {
  const box = ui.tonight; clear(box);
  box.append(h("h3", {}, "Tonight's best targets"));
  if (state.settings?.location?.lat === null || state.settings?.location?.lat === undefined) {
    box.append(h("div", { class: "warnbox" }, "Set your location (Settings > Location or the setup wizard) to see what's up tonight."));
    return;
  }
  const chosen = new Set(state.settings?.mount?.tonight_interests || INTERESTS.map((i) => i[0]));
  const filters = h("div", { class: "row" }, ...INTERESTS.map(([k, l]) => h("label", { class: "check tiny" }, h("input", { type: "checkbox", checked: chosen.has(k), onchange: (e) => {
    e.target.checked ? chosen.add(k) : chosen.delete(k); state.saveSettings({ mount: { tonight_interests: [...chosen] } }); loadTonight();
  } }), l)));
  box.append(filters, h("div", { class: "tiny" }, "Loading..."));
  try {
    tonightData = await api.get("/api/catalog/tonight?" + api.q({ interests: [...chosen].join(","), limit: 30 }), { quiet: true });
  } catch (e) { box.lastChild.textContent = e.message; return; }
  box.lastChild.remove();
  const w = tonightData.window || {};
  box.append(h("div", { class: "tiny" }, `Dark (Sun 12° below the horizon) from ${shortTime(w.dark_start)} to ${shortTime(w.dark_end)}. Moon ${Math.round((tonightData.moon?.illumination || 0) * 100)}% lit.`));
  const list = h("div");
  for (const t of tonightData.targets) {
    const cv = h("canvas", { class: "alt" });
    const row = h("div", { class: "target-row", onclick: () => pick(t.id || t.name) },
      h("div", { style: { flex: 1, minWidth: 0 } }, h("b", {}, t.label || t.name), h("div", { class: "tiny" },
        `${t.kind}${t.mag !== null && t.mag !== undefined ? `, mag ${t.mag}` : ""} - highest ${t.max_alt}° at ${shortTime(t.best_time)}, ${t.hours_up} h up` +
        (t.field_rotation_deg_per_min > 0.4 && t.group !== "planet" && t.group !== "moon" ? " - fast field rotation at best time" : ""))),
      cv, h("span", { class: "chip" }, Math.round(t.score)));
    list.append(row);
    requestAnimationFrame(() => drawAltitude(cv, t.altitudes, { limit: 25 }));
  }
  box.append(list);
}

async function pick(name) {
  if (!name) return;
  try { picked = await api.get("/api/catalog/position?" + api.q({ name })); } catch { return; }
  renderTarget();
  ui.center.scrollTo({ top: 0, behavior: "smooth" });
}

function renderTarget() {
  const box = ui.target; clear(box);
  if (!picked) { box.append(h("h3", {}, "Target"), h("div", { class: "tiny" }, "Pick a target below.")); return; }
  const p = picked;
  const low = p.alt !== undefined && p.alt < (st?.alt_limit ?? 10);
  const sun = /^sun$/i.test(p.name);
  box.append(h("div", { class: "row" }, h("h2", { style: { margin: 0 } }, p.name), p.kind ? h("span", { class: "chip" }, p.kind) : "", p.const ? h("span", { class: "chip" }, p.const) : ""),
    h("div", { class: "kv", style: { marginTop: "6px" } },
      h("span", {}, "RA / Dec"), h("span", {}, `${p.ra_str}  /  ${p.dec_str}`),
      p.alt !== undefined ? h("span", {}, "Now") : "", p.alt !== undefined ? h("span", {}, `${fmtNum(p.alt, 1)}° up, ${compass(p.az)} (azimuth ${fmtNum(p.az, 0)}°)`) : "",
      p.mag !== undefined && p.mag !== null ? h("span", {}, "Brightness") : "", p.mag !== undefined && p.mag !== null ? h("span", {}, `magnitude ${p.mag}`) : "",
      p.diameter_arcsec ? h("span", {}, "Size") : "", p.diameter_arcsec ? h("span", {}, `${fmtNum(p.diameter_arcsec, 1)}"`) : "",
      p.size_arcmin ? h("span", {}, "Size") : "", p.size_arcmin ? h("span", {}, `${fmtNum(p.size_arcmin, 1)}'`) : ""),
    sun ? h("div", { class: "dangerbox" }, "Never point at the Sun without a proper full-aperture solar filter. GoTo to the Sun is blocked.") : "",
    low ? h("div", { class: "warnbox" }, "Below your altitude limit right now - GoTo is not allowed until it rises.") : "",
    p.rotation && !["Planet", "Moon"].includes(p.kind) && state.settings?.equipment?.mount_type !== "eq"
      ? h("div", { class: p.rotation.level === "ok" ? "okbox tiny" : p.rotation.level === "warn" ? "warnbox tiny" : "dangerbox tiny" }, term("Field rotation"), ": ", p.rotation.text) : "",
    h("div", { class: "row", style: { marginTop: "8px" } },
      h("button", { class: "primary big", disabled: !st?.connected || low || sun, onclick: () => gotoTarget(p) }, "GoTo"),
      h("button", { disabled: !st?.connected || sun, "data-tip": "Centre the object in the eyepiece/camera by hand first, then press Sync. This teaches the mount where it really points.", onclick: () => syncTarget(p) }, "Sync (I'm centred on it)"),
      h("button", { disabled: !st?.connected || sun, "data-tip": "Takes pictures with the camera, plate-solves them and nudges the mount until the target is in the middle.", onclick: () => center(p) }, "Plate-solve & centre"),
      h("button", { class: "ghost", onclick: async () => { try { await api.post("/api/capture/target", { target: p.name }); } catch { /* */ } go("capture"); } }, "Capture this →")),
    ui.centerBox = h("div"));
}

async function gotoTarget(p) {
  try {
    let r = await api.post("/api/mount/goto", { name: p.name, ra: p.ra, dec: p.dec, label: p.name });
    if (r.needs_confirmation) {
      if (!(await confirmDialog(`GoTo ${p.name}?`, r.message, { ok: "Start the move" }))) return;
      r = await api.post("/api/mount/goto", { ra: p.ra, dec: p.dec, label: p.name, confirm: r.token });
    }
    if (r.refused) { modal({ title: "GoTo not started", body: h("p", {}, r.message), buttons: [{ label: "OK", class: "primary" }] }); return; }
    if (r.started) toast(`Slewing to ${p.name}. Press STOP (or Esc) to halt.`, "ok");
  } catch { /* toast */ }
}

async function syncTarget(p) {
  if (!(await confirmDialog(`Sync on ${p.name}?`, `Only sync when ${p.name} is centred in your view. The mount will treat its current position as ${p.name}.`, { ok: "Sync" }))) return;
  await api.post("/api/mount/sync", { ra: p.ra, dec: p.dec, label: p.name });
  toast("Synced - GoTo accuracy improves with each sync", "ok");
}

async function center(p) {
  if (!(await confirmDialog("Plate-solve and centre", `The camera takes short exposures and the mount makes small corrections until ${p.name} is centred. Make sure the camera is connected and focused. Continue?`, { ok: "Start" }))) return;
  try {
    const { job } = await api.post("/api/mount/center", { ra: p.ra, dec: p.dec, label: p.name, exposure_s: 2 });
    const r = await runJob(job, ui.centerBox, { title: `Centring ${p.name}` });
    ui.centerBox.append(h("div", { class: r.centered ? "okbox" : "warnbox" }, r.message,
      ...(r.steps || []).map((s, i) => h("div", { class: "tiny" }, `Step ${s.iteration || i + 1}: ${s.error_arcmin !== undefined ? fmtNum(s.error_arcmin, 1) + "' away from the target" : s.message || ""}`))));
  } catch { /* shown */ }
}

// ------------------------------------------------------------------ helpers
function compass(az) { return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(((az % 360) + 360) % 360 / 45) % 8]; }
function fmtRa(deg) { const hrs = (((deg / 15) % 24) + 24) % 24; const hh = Math.floor(hrs), mm = Math.floor((hrs - hh) * 60), ss = ((hrs - hh) * 60 - mm) * 60; return `${hh}h ${String(mm).padStart(2, "0")}m ${ss.toFixed(0).padStart(2, "0")}s`; }
function fmtDec(deg) { const s = deg < 0 ? "-" : "+"; const a = Math.abs(deg); const d = Math.floor(a), m = Math.floor((a - d) * 60), ss = ((a - d) * 60 - m) * 60; return `${s}${d}° ${String(m).padStart(2, "0")}' ${ss.toFixed(0).padStart(2, "0")}"`; }
function shortTime(iso) { if (!iso) return "?"; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
