// Small UI toolkit: element builder, toasts, dialogs, tooltips, settings forms, folder browser, formatters.
import { explain } from "./glossary.js";

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "html") el.innerHTML = v;
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

// A term with a plain-language tooltip.
export function term(word, label) { return h("span", { class: "term", "data-term": word }, label || word); }

// ----------------------------------------------------------- formatting
export function fmtBytes(n) {
  if (n === null || n === undefined) return "?";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0; let v = n;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(v < 10 ? 2 : 1)) + " " + u[i];
}
export function fmtDur(s) {
  if (s === null || s === undefined || !isFinite(s)) return "";
  s = Math.round(s);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
  return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
}
export function fmtNum(v, d = 2) { return v === null || v === undefined || isNaN(v) ? "-" : Number(v).toFixed(d); }
export function fmtDate(iso) { if (!iso) return ""; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString(); }
export function basename(p) { return (p || "").split(/[\\/]/).pop(); }

// ------------------------------------------------------------- toasts
export function toast(msg, kind = "info", ms) {
  const box = document.getElementById("toasts");
  if (!box) return;
  const t = h("div", { class: `toast ${kind}` }, msg);
  box.append(t);
  setTimeout(() => t.remove(), ms || (kind === "error" ? 9000 : 4500));
  t.addEventListener("click", () => t.remove());
}

// ------------------------------------------------------------- dialogs
export function modal({ title, body, buttons = [], wide = false, onClose }) {
  const back = h("div", { class: "modal-back" });
  const box = h("div", { class: "modal" + (wide ? " wide" : ""), role: "dialog", "aria-modal": "true" });
  const footer = h("footer");
  const close = (v) => { back.remove(); document.removeEventListener("keydown", onKey); onClose && onClose(v); };
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); close(null); } };
  for (const b of buttons) {
    footer.append(h("button", { class: b.class || "", onclick: async () => {
      if (b.action) { const r = await b.action(); if (r === false) return; }
      close(b.value !== undefined ? b.value : b.label);
    } }, b.label));
  }
  box.append(h("header", {}, title || ""), h("div", { class: "body" }, body), footer);
  back.append(box);
  back.addEventListener("mousedown", (e) => { if (e.target === back) close(null); });
  document.body.append(back);
  document.addEventListener("keydown", onKey);
  const first = footer.querySelector("button.primary") || footer.querySelector("button");
  first && setTimeout(() => first.focus(), 30);
  return { close, el: box };
}

export function confirmDialog(title, message, { ok = "OK", cancel = "Cancel", danger = false, details } = {}) {
  return new Promise((resolve) => {
    modal({
      title,
      body: h("div", {}, typeof message === "string" ? h("p", {}, message) : message, details || ""),
      buttons: [{ label: cancel, value: false }, { label: ok, class: danger ? "danger" : "primary", value: true }],
      onClose: (v) => resolve(v === true),
    });
  });
}

export function promptDialog(title, label, value = "") {
  return new Promise((resolve) => {
    const inp = h("input", { value, style: { width: "100%" } });
    modal({
      title, body: h("div", { class: "field" }, h("label", {}, label), inp),
      buttons: [{ label: "Cancel", value: null }, { label: "OK", class: "primary", action: () => { resolve(inp.value); } }],
      onClose: (v) => { if (v === null) resolve(null); },
    });
    setTimeout(() => inp.focus(), 50);
  });
}

// ------------------------------------------------------------- tooltips
let tipEl = null;
export function initTooltips() {
  document.addEventListener("mouseover", (e) => {
    const t = e.target.closest("[data-term],[data-tip]");
    if (!t) { hideTip(); return; }
    const text = t.dataset.tip || explain(t.dataset.term);
    if (!text) return;
    showTip(text, e.clientX, e.clientY);
  });
  document.addEventListener("mousemove", (e) => { if (tipEl) placeTip(e.clientX, e.clientY); });
  document.addEventListener("scroll", hideTip, true);
}
function showTip(text, x, y) {
  if (!tipEl) { tipEl = h("div", { class: "tip" }); document.body.append(tipEl); }
  tipEl.textContent = text; placeTip(x, y);
}
function placeTip(x, y) {
  const r = tipEl.getBoundingClientRect();
  tipEl.style.left = Math.min(x + 14, innerWidth - r.width - 8) + "px";
  tipEl.style.top = Math.min(y + 16, innerHeight - r.height - 8) + "px";
}
function hideTip() { if (tipEl) { tipEl.remove(); tipEl = null; } }

// --------------------------------------------------------- settings forms
// Builds controls from a list of parameter specs ({name,label,kind,default,min,max,step,choices,help,advanced}).
export function paramForm(specs, values, onChange, { showAdvanced = true } = {}) {
  const box = h("div", { class: "pform" });
  for (const p of specs) {
    if (p.kind === "file") continue;
    if (p.advanced && !showAdvanced) continue;
    const v = values[p.name] !== undefined ? values[p.name] : p.default;
    const help = p.help ? { "data-tip": p.help } : {};
    const wrap = h("div", { class: "field" + (p.advanced ? " adv" : "") });
    if (p.kind === "bool") {
      const cb = h("input", { type: "checkbox", checked: !!v, onchange: () => onChange(p.name, cb.checked) });
      wrap.append(h("label", { class: "check", ...help }, cb, p.label));
    } else if (p.kind === "choice") {
      const sel = h("select", { onchange: () => onChange(p.name, sel.value) },
        ...(p.choices || []).map((c) => { const [val, lab] = Array.isArray(c) ? c : [c, prettify(c)]; return h("option", { value: val, selected: val === v }, lab); }));
      wrap.append(h("label", help, p.label), sel);
    } else if (p.kind === "points") {
      wrap.append(h("label", help, p.label), curveEditor(v || p.default, (pts) => onChange(p.name, pts)));
    } else if (p.kind === "text") {
      const inp = h("input", { value: v || "", onchange: () => onChange(p.name, inp.value) });
      wrap.append(h("label", help, p.label), inp);
    } else {
      const step = p.step || (p.kind === "int" ? 1 : 0.01);
      const num = h("input", { type: "number", value: v, step, min: p.min, max: p.max });
      const rng = h("input", { type: "range", value: v, step, min: p.min ?? 0, max: p.max ?? 100 });
      let timer;
      const set = (val, from) => {
        if (from !== num) num.value = val;
        if (from !== rng) rng.value = val;
        clearTimeout(timer);
        timer = setTimeout(() => onChange(p.name, p.kind === "int" ? parseInt(val) : parseFloat(val)), 120);
      };
      rng.addEventListener("input", () => set(rng.value, rng));
      num.addEventListener("change", () => set(num.value, num));
      rng.addEventListener("dblclick", () => set(p.default, null));
      wrap.append(h("label", help, h("span", {}, p.label)), h("div", { class: "slider" }, rng, num));
    }
    box.append(wrap);
  }
  return box;
}
export function prettify(s) { return String(s).replace(/_/g, " ").replace(/\b\w/, (c) => c.toUpperCase()); }

// A small interactive curves editor (points from 0..1, drag to move, double-click to add/remove).
export function curveEditor(points, onChange) {
  const W = 260, H = 200;
  const cv = h("canvas", { width: W, height: H, class: "chart", style: { height: H + "px", width: "100%", cursor: "crosshair" } });
  let pts = (points || [[0, 0], [1, 1]]).map((p) => [...p]);
  let drag = -1;
  const toXY = (p) => [p[0] * (W - 1), (1 - p[1]) * (H - 1)];
  const draw = () => {
    const ctx = cv.getContext("2d");
    const css = getComputedStyle(document.documentElement);
    ctx.clearRect(0, 0, W, H);
    ctx.strokeStyle = css.getPropertyValue("--border"); ctx.lineWidth = 1;
    for (let i = 1; i < 4; i++) { ctx.beginPath(); ctx.moveTo(i * W / 4, 0); ctx.lineTo(i * W / 4, H); ctx.moveTo(0, i * H / 4); ctx.lineTo(W, i * H / 4); ctx.stroke(); }
    ctx.strokeStyle = css.getPropertyValue("--fg3"); ctx.beginPath(); ctx.moveTo(0, H); ctx.lineTo(W, 0); ctx.stroke();
    const sorted = [...pts].sort((a, b) => a[0] - b[0]);
    ctx.strokeStyle = css.getPropertyValue("--accent"); ctx.lineWidth = 2; ctx.beginPath();
    for (let x = 0; x <= W; x += 2) { const y = interp(sorted, x / (W - 1)); const py = (1 - y) * (H - 1); x === 0 ? ctx.moveTo(x, py) : ctx.lineTo(x, py); }
    ctx.stroke();
    ctx.fillStyle = css.getPropertyValue("--fg");
    for (const p of pts) { const [x, y] = toXY(p); ctx.beginPath(); ctx.arc(x, y, 5, 0, 7); ctx.fill(); }
  };
  const pos = (e) => { const r = cv.getBoundingClientRect(); return [(e.clientX - r.left) / r.width, 1 - (e.clientY - r.top) / r.height]; };
  cv.addEventListener("pointerdown", (e) => {
    const [x, y] = pos(e);
    drag = pts.findIndex((p) => Math.hypot(p[0] - x, p[1] - y) < 0.05);
    if (drag < 0) { pts.push([x, y]); drag = pts.length - 1; }
    cv.setPointerCapture(e.pointerId); draw();
  });
  cv.addEventListener("pointermove", (e) => {
    if (drag < 0) return;
    let [x, y] = pos(e); x = Math.min(1, Math.max(0, x)); y = Math.min(1, Math.max(0, y));
    if (drag === 0 && pts[0][0] === 0) x = 0;
    pts[drag] = [x, y]; draw();
  });
  cv.addEventListener("pointerup", () => { if (drag >= 0) { drag = -1; onChange(pts.map((p) => [+p[0].toFixed(4), +p[1].toFixed(4)])); } });
  cv.addEventListener("dblclick", (e) => {
    const [x, y] = pos(e);
    const i = pts.findIndex((p) => Math.hypot(p[0] - x, p[1] - y) < 0.05);
    if (i >= 0 && pts.length > 2) { pts.splice(i, 1); draw(); onChange(pts); }
  });
  setTimeout(draw, 0);
  const reset = h("button", { class: "small ghost", onclick: () => { pts = [[0, 0], [1, 1]]; draw(); onChange(pts); } }, "Reset curve");
  return h("div", {}, cv, h("div", { class: "row tiny" }, "Drag points; click to add; double-click to remove.", reset));
}
function interp(sorted, x) {
  // Monotone cubic (Fritsch-Carlson), matching the server's PCHIP closely enough for display.
  const n = sorted.length;
  if (n === 0) return x;
  if (x <= sorted[0][0]) return sorted[0][1];
  if (x >= sorted[n - 1][0]) return sorted[n - 1][1];
  let i = 0; while (i < n - 2 && x > sorted[i + 1][0]) i++;
  const [x0, y0] = sorted[i], [x1, y1] = sorted[i + 1];
  const t = (x - x0) / Math.max(x1 - x0, 1e-6);
  return y0 + (y1 - y0) * (t * t * (3 - 2 * t));
}

// ---------------------------------------------------------- folder browser
export function pickPath({ title = "Choose a file", dirs = false, multiple = false, start = "", exts = null } = {}) {
  return new Promise((resolve) => {
    let cur = start;
    const list = h("div", { class: "listbox", style: { maxHeight: "50vh" } });
    const pathEl = h("input", { value: cur, style: { flex: 1 } });
    const chosen = new Set();
    const info = h("div", { class: "tiny" });
    const load = async (p) => {
      const { api } = await import("./api.js");
      let res;
      try { res = await api.get("/api/fs/list?" + api.q({ path: p, show_files: !dirs })); } catch { return; }
      cur = res.path; pathEl.value = cur; clear(list); chosen.clear();
      info.textContent = res.disk ? `${fmtBytes(res.disk.free)} free of ${fmtBytes(res.disk.total)}` : "";
      if (res.parent !== null && res.path) list.append(h("div", { class: "item", onclick: () => load(res.parent) }, "⬆  .. (up)"));
      for (const e of res.entries) {
        if (!e.dir && exts && !exts.some((x) => e.name.toLowerCase().endsWith(x))) continue;
        const it = h("div", { class: "item" }, e.dir ? "📁" : "📄", h("span", { style: { flex: 1 } }, e.name), e.size !== null && e.size !== undefined ? h("small", {}, fmtBytes(e.size)) : "");
        it.addEventListener("click", (ev) => {
          if (e.dir) { load(e.path); return; }
          if (!multiple) { chosen.clear(); $$(".item.sel", list).forEach((x) => x.classList.remove("sel")); }
          if (chosen.has(e.path)) { chosen.delete(e.path); it.classList.remove("sel"); } else { chosen.add(e.path); it.classList.add("sel"); }
        });
        it.addEventListener("dblclick", () => { if (!e.dir) { chosen.clear(); chosen.add(e.path); m.close("ok"); } });
        list.append(it);
      }
    };
    const body = h("div", {}, h("div", { class: "row" }, pathEl, h("button", { onclick: () => load(pathEl.value) }, "Go")), info, list,
      multiple ? h("p", { class: "tiny" }, "Click files to select several. Tip: the whole folder can be used too.") : "");
    const m = modal({
      title, body, wide: true,
      buttons: [
        { label: "Cancel", value: null },
        ...(dirs || multiple ? [{ label: dirs ? "Use this folder" : "Use all files in this folder", value: "folder" }] : []),
        ...(!dirs ? [{ label: "Choose selected", class: "primary", value: "ok" }] : []),
      ],
      onClose: (v) => {
        if (v === "folder") resolve(multiple ? [cur] : cur);
        else if (v === "ok") resolve(multiple ? [...chosen] : [...chosen][0] || null);
        else resolve(null);
      },
    });
    load(cur);
  });
}

// ------------------------------------------------------------- misc
export function stars(n, onSet) {
  const el = h("span", { class: "stars", title: "Your rating" });
  const draw = (v) => { el.textContent = "★".repeat(v) + "☆".repeat(5 - v); };
  draw(n || 0);
  if (onSet) el.addEventListener("click", (e) => {
    const r = el.getBoundingClientRect();
    const v = Math.min(5, Math.max(0, Math.ceil(((e.clientX - r.left) / r.width) * 5)));
    draw(v); onSet(v);
  });
  return el;
}

export function progressBar() {
  const inner = h("div");
  const bar = h("div", { class: "progress" }, inner);
  const label = h("div", { class: "tiny" });
  const wrap = h("div", { class: "col" }, bar, label);
  wrap.set = (frac, text) => { inner.style.width = Math.round((frac || 0) * 100) + "%"; if (text !== undefined) label.textContent = text; };
  return wrap;
}

export function section(title, content, { open = true, adv = false, id } = {}) {
  return h("details", { class: "panel" + (adv ? " adv" : ""), open, id }, h("summary", {}, title), content);
}

export function debounce(fn, ms = 250) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
