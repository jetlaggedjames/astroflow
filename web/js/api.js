// Talking to the AstroFlow server.
import { toast } from "./ui.js";

async function request(method, url, body, opts = {}) {
  const init = { method, headers: {} };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { init.body = JSON.stringify(body); init.headers["Content-Type"] = "application/json"; }
  let r;
  try { r = await fetch(url, init); }
  catch (e) { if (!opts.quiet) toast("Cannot reach AstroFlow's engine. Is it still running?", "error"); throw e; }
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.detail || msg; } catch { /* not json */ }
    const err = new Error(msg); err.status = r.status;
    if (!opts.quiet) toast(msg, "error");
    throw err;
  }
  const ct = r.headers.get("content-type") || "";
  if (ct.includes("application/json")) return r.json();
  if (ct.startsWith("image/")) return r.blob();
  return r.text();
}

export const api = {
  get: (url, opts) => request("GET", url, undefined, opts),
  post: (url, body = {}, opts) => request("POST", url, body, opts),
  patch: (url, body = {}, opts) => request("PATCH", url, body, opts),
  del: (url, opts) => request("DELETE", url, undefined, opts),
  q: (params) => {
    const u = new URLSearchParams();
    for (const [k, v] of Object.entries(params || {})) if (v !== undefined && v !== null && v !== "") u.set(k, v);
    return u.toString();
  },
};

// Wait for a background task, reporting progress. Resolves with the task's result.
export async function waitJob(jobId, onProgress) {
  while (true) {
    const j = await api.get(`/api/jobs/${jobId}`, { quiet: true });
    onProgress && onProgress(j);
    if (j.status === "done") return j.result;
    if (j.status === "error") { const e = new Error(j.error || "Task failed"); e.job = j; throw e; }
    if (j.status === "cancelled") { const e = new Error("Cancelled"); e.cancelled = true; throw e; }
    await new Promise((res) => setTimeout(res, j.status === "queued" ? 500 : 350));
  }
}

export async function uploadFiles(files, subfolder = "", onProgress) {
  const out = [];
  let i = 0;
  for (const f of files) {
    i++;
    onProgress && onProgress(i, files.length, f.name);
    const fd = new FormData();
    fd.append("file", f);
    fd.append("subfolder", subfolder);
    out.push(await api.post("/api/upload", fd));
  }
  return out;
}
