// Helpers shared by the tabs: hints, background-task runner, file choosers, image URLs.
import { api, waitJob } from "./api.js";
import { state } from "./state.js";
import { basename, clear, fmtBytes, fmtDur, h, pickPath, progressBar, toast } from "./ui.js";

// A blue "what do I do next?" box, shown in Beginner mode only.
export function hint(...content) { return h("div", { class: "hint beg" }, ...content); }

export const previewUrl = (path, max = 1400, frame) =>
  "/api/image/preview?" + api.q({ path, max, frame, t: state.status?.version || "" });

// Run a background task with a progress bar + cancel button inside `box`. Resolves with the result.
export async function runJob(jobId, box, { title } = {}) {
  const bar = progressBar();
  const cancel = h("button", { class: "small", onclick: () => api.post(`/api/jobs/${jobId}/cancel`) }, "Cancel");
  const wrap = h("div", { class: "panel" }, title ? h("b", {}, title) : "", bar, h("div", { class: "row" }, h("span", { class: "spacer" }), cancel));
  if (box) { clear(box); box.append(wrap); }
  try {
    return await waitJob(jobId, (j) => bar.set(j.progress,
      `${j.message || j.status}${j.eta_s ? ` - about ${fmtDur(j.eta_s)} left` : ""}${j.elapsed_s ? ` (${fmtDur(j.elapsed_s)} so far)` : ""}`));
  } catch (e) {
    if (e.cancelled) toast("Cancelled", "warn");
    else toast(e.message, "error");
    throw e;
  } finally {
    if (box) clear(box);
  }
}

// Choose image/video files: from the Library selection, or by browsing.
export async function chooseFiles({ title = "Choose files", multiple = true, exts } = {}) {
  const res = await pickPath({ title, multiple, exts: exts || [".ser", ".avi", ".fits", ".fit", ".fts", ".tif", ".tiff", ".png", ".jpg", ".jpeg", ".xisf", ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf"] });
  if (!res) return null;
  return Array.isArray(res) ? res : [res];
}

// A compact "what am I working on" box with a change button.
export function sourceBox(onChange) {
  const box = h("div", { class: "panel" });
  let sources = [];
  const render = () => {
    clear(box);
    box.append(h("h3", {}, "Files"));
    if (!sources.length) box.append(h("div", { class: "tiny" }, "Nothing chosen yet."));
    else {
      const total = sources.length;
      box.append(h("div", { class: "col" },
        ...sources.slice(0, 4).map((s) => h("div", { class: "mono", title: s, style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" } }, basename(s))),
        total > 4 ? h("div", { class: "tiny" }, `... and ${total - 4} more`) : ""));
    }
    box.append(h("div", { class: "row", style: { marginTop: "6px" } },
      h("button", { class: "small", onclick: async () => { const f = await chooseFiles(); if (f) set(f, true); } }, "Choose files..."),
      state.selection.length ? h("button", { class: "small", onclick: () => set([...state.selection], true) }, `Use Library selection (${state.selection.length})`) : "",
      state.lastRecording ? h("button", { class: "small", onclick: () => set([state.lastRecording.path], true) }, "Last recording") : ""));
  };
  const set = (s, user) => { sources = [...s]; render(); if (user) onChange(sources); };
  box.set = set;
  box.get = () => sources;
  render();
  return box;
}

export function statTiles(pairs) {
  return h("div", { class: "stats" }, ...pairs.filter(Boolean).map(([v, label, tip]) => h("div", { class: "stat", "data-tip": tip }, h("b", {}, v), h("span", {}, label))));
}

export function sizeText(bytes) { return fmtBytes(bytes); }

// Ask the server to open a folder in Explorer (main computer only).
export async function reveal(path) {
  if (!state.local) { toast("Files are on the main computer. Use Download to copy them here.", "warn"); return; }
  try { await api.post("/api/fs/reveal", { path }); } catch { /* toast shown */ }
}
