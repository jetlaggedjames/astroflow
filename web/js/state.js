// Shared application state + a tiny event bus, so tabs can hand work to each other.
import { api } from "./api.js";

const listeners = {};
export const state = {
  settings: null,
  status: null,
  local: true,
  project: null,          // the open project (object from the server)
  lastRecording: null,    // set by Capture so Analyze can pick it up
  selection: [],          // file paths chosen in the Library
  ops: null,              // processing tool definitions (loaded once)

  on(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); },
  emit(evt, data) { for (const fn of listeners[evt] || []) { try { fn(data); } catch (e) { console.error(e); } } },

  get mode() { return document.documentElement.dataset.mode; },
  get advanced() { return document.documentElement.dataset.mode === "advanced"; },

  async saveSettings(patch) {
    this.settings = await api.patch("/api/settings", patch);
    this.emit("settings", this.settings);
    return this.settings;
  },

  async openProject(idOrProject) {
    this.project = typeof idOrProject === "string" ? await api.get(`/api/projects/${idOrProject}`) : idOrProject;
    this.emit("project", this.project);
    return this.project;
  },

  async refreshProject() {
    if (this.project) this.project = await api.get(`/api/projects/${this.project.id}`, { quiet: true });
    this.emit("project", this.project);
    return this.project;
  },

  // Make sure a project exists for these source files (creates one if needed).
  async projectFor(sources, name, target) {
    const same = this.project && JSON.stringify([...(this.project.sources || [])].sort()) === JSON.stringify([...sources].sort());
    if (same) return this.project;
    const p = await api.post("/api/projects", { name, sources, target: target || {} });
    return this.openProject(p);
  },

  async loadOps() {
    if (!this.ops) this.ops = await api.get("/api/process/ops");
    return this.ops;
  },
};

export function go(tab, data) { state.emit("navigate", { tab, data }); }
