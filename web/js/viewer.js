// Image viewer: zoom/pan (mouse, wheel, touch pinch), fit / 100%, before-after split, overlays, brush painting.
import { h } from "./ui.js";

export class Viewer {
  constructor({ onPaint, onClick, onSelectRect } = {}) {
    this.el = h("div", { class: "viewer astro" });
    this.canvas = h("canvas");
    this.svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    this.svg.setAttribute("class", "overlay");
    this.info = h("div", { class: "vinfo" });
    this.busy = h("div", { class: "busy" }, "Working...");
    this.el.append(this.canvas, this.svg, this.info, this.busy);
    this.img = null; this.before = null;
    this.zoom = 1; this.ox = 0; this.oy = 0; this.fit = true;
    this.split = null;           // 0..1 when comparing before/after
    this.tool = "pan";           // pan | brush | erase | rect
    this.brush = { radius: 0.03, hardness: 0.5, strength: 1 };
    this.onPaint = onPaint; this.onClick = onClick; this.onSelectRect = onSelectRect;
    this.overlays = [];          // functions (svg, view) => void
    this.fullW = 0; this.fullH = 0;   // full-resolution size the preview stands for
    this._bind();
    new ResizeObserver(() => this.draw()).observe(this.el);
  }

  setBusy(on, text) { this.busy.classList.toggle("on", !!on); if (text) this.busy.textContent = text; }

  async setImage(src, { keepView = true, fullW, fullH } = {}) {
    const img = await loadImage(src);
    const first = !this.img;
    const sizeChanged = this.img && (this.img.naturalWidth !== img.naturalWidth || this.img.naturalHeight !== img.naturalHeight);
    this.img = img;
    this.fullW = fullW || img.naturalWidth; this.fullH = fullH || img.naturalHeight;
    if (first || !keepView || this.fit || sizeChanged) this.fitView();
    this.draw();
  }
  async setBefore(src) { this.before = src ? await loadImage(src) : null; this.draw(); }
  clear() { this.img = null; this.before = null; this.draw(); }

  fitView() {
    if (!this.img) return;
    const r = this.el.getBoundingClientRect();
    const z = Math.min(r.width / this.img.naturalWidth, r.height / this.img.naturalHeight) || 1;
    this.zoom = z; this.ox = (r.width - this.img.naturalWidth * z) / 2; this.oy = (r.height - this.img.naturalHeight * z) / 2;
    this.fit = true; this.draw();
  }
  actualSize() {
    // 100% of the *full resolution* image (the preview may be smaller).
    if (!this.img) return;
    const r = this.el.getBoundingClientRect();
    const z = this.fullW ? this.fullW / this.img.naturalWidth : 1;
    const cx = (r.width / 2 - this.ox) / this.zoom, cy = (r.height / 2 - this.oy) / this.zoom;
    this.zoom = z; this.ox = r.width / 2 - cx * z; this.oy = r.height / 2 - cy * z; this.fit = false; this.draw();
  }
  zoomAt(factor, x, y) {
    const nz = Math.min(Math.max(this.zoom * factor, 0.02), 40);
    this.ox = x - (x - this.ox) * (nz / this.zoom); this.oy = y - (y - this.oy) * (nz / this.zoom);
    this.zoom = nz; this.fit = false; this.draw();
  }
  toImage(x, y) { return [(x - this.ox) / this.zoom, (y - this.oy) / this.zoom]; }   // preview pixel coords
  toScreen(ix, iy) { return [ix * this.zoom + this.ox, iy * this.zoom + this.oy]; }

  draw() {
    const r = this.el.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const W = Math.max(1, Math.round(r.width * dpr)), H = Math.max(1, Math.round(r.height * dpr));
    if (this.canvas.width !== W || this.canvas.height !== H) { this.canvas.width = W; this.canvas.height = H; }
    const ctx = this.canvas.getContext("2d");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (!this.img) { this.info.textContent = ""; this._overlay(r); return; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.imageSmoothingEnabled = this.zoom < 2;
    const iw = this.img.naturalWidth, ih = this.img.naturalHeight;
    if (this.split !== null && this.before) {
      const sx = r.width * this.split;
      ctx.save(); ctx.beginPath(); ctx.rect(0, 0, sx, r.height); ctx.clip();
      ctx.drawImage(this.before, this.ox, this.oy, iw * this.zoom, ih * this.zoom); ctx.restore();
      ctx.save(); ctx.beginPath(); ctx.rect(sx, 0, r.width - sx, r.height); ctx.clip();
      ctx.drawImage(this.img, this.ox, this.oy, iw * this.zoom, ih * this.zoom); ctx.restore();
    } else {
      ctx.drawImage(this.img, this.ox, this.oy, iw * this.zoom, ih * this.zoom);
    }
    const pct = Math.round(this.zoom * (iw / (this.fullW || iw)) * 100);
    this.info.textContent = `${this.fullW} x ${this.fullH}  |  ${pct}%`;
    this._overlay(r);
  }

  _overlay(r) {
    const svg = this.svg;
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    svg.setAttribute("viewBox", `0 0 ${r.width} ${r.height}`);
    if (this.split !== null && this.before) {
      const x = r.width * this.split;
      svg.append(svgEl("line", { x1: x, x2: x, y1: 0, y2: r.height, stroke: "var(--accent)", "stroke-width": 2 }));
      svg.append(svgText(x - 60, 20, "Before"), svgText(x + 10, 20, "After"));
    }
    for (const fn of this.overlays) { try { fn(svg, this); } catch (e) { console.warn(e); } }
    if (this._rect) {
      const [a, b] = [this._rect[0], this._rect[1]];
      svg.append(svgEl("rect", { x: Math.min(a[0], b[0]), y: Math.min(a[1], b[1]), width: Math.abs(a[0] - b[0]), height: Math.abs(a[1] - b[1]),
        fill: "none", stroke: "var(--accent)", "stroke-dasharray": "6 4", "stroke-width": 2 }));
    }
    if ((this.tool === "brush" || this.tool === "erase") && this._hover) {
      const rad = this.brush.radius * (this.img ? this.img.naturalWidth : 1) * this.zoom;
      svg.append(svgEl("circle", { cx: this._hover[0], cy: this._hover[1], r: rad, fill: "none", stroke: this.tool === "erase" ? "#f55" : "#5f5", "stroke-width": 1.5 }));
    }
  }

  _bind() {
    const el = this.el;
    let dragging = false, last = null, pts = new Map(), pinch = null, stroke = null, splitDrag = false;
    el.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      this.zoomAt(e.deltaY < 0 ? 1.2 : 1 / 1.2, e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    el.addEventListener("pointerdown", (e) => {
      el.setPointerCapture(e.pointerId);
      const r = el.getBoundingClientRect();
      const x = e.clientX - r.left, y = e.clientY - r.top;
      pts.set(e.pointerId, [x, y]);
      if (pts.size === 2) {
        const [a, b] = [...pts.values()];
        pinch = { d: Math.hypot(a[0] - b[0], a[1] - b[1]), z: this.zoom };
        stroke = null; return;
      }
      if (this.split !== null && Math.abs(x - r.width * this.split) < 12) { splitDrag = true; return; }
      if ((this.tool === "brush" || this.tool === "erase") && this.img) {
        stroke = { mode: this.tool === "erase" ? "erase" : "add", points: [this._norm(x, y)], radius: this.brush.radius,
                   hardness: this.brush.hardness, strength: this.brush.strength };
        return;
      }
      if (this.tool === "rect") { this._rect = [[x, y], [x, y]]; return; }
      dragging = true; last = [x, y]; el.classList.add("dragging");
      this._down = [x, y];
    });
    el.addEventListener("pointermove", (e) => {
      const r = el.getBoundingClientRect();
      const x = e.clientX - r.left, y = e.clientY - r.top;
      this._hover = [x, y];
      if (pts.has(e.pointerId)) pts.set(e.pointerId, [x, y]);
      if (pinch && pts.size === 2) {
        const [a, b] = [...pts.values()];
        const d = Math.hypot(a[0] - b[0], a[1] - b[1]);
        this.zoomAt((pinch.z * d / pinch.d) / this.zoom, (a[0] + b[0]) / 2, (a[1] + b[1]) / 2);
        return;
      }
      if (splitDrag) { this.split = Math.min(0.98, Math.max(0.02, x / r.width)); this.draw(); return; }
      if (stroke) { stroke.points.push(this._norm(x, y)); this._drawStroke(stroke); this.draw(); return; }
      if (this._rect) { this._rect[1] = [x, y]; this.draw(); return; }
      if (dragging) { this.ox += x - last[0]; this.oy += y - last[1]; last = [x, y]; this.fit = false; this.draw(); return; }
      if (this.tool !== "pan") this.draw();
      if (this.img && this.onHover) { const [ix, iy] = this.toImage(x, y); this.onHover(ix, iy); }
    });
    const end = (e) => {
      pts.delete(e.pointerId);
      if (pts.size < 2) pinch = null;
      if (splitDrag) { splitDrag = false; return; }
      if (stroke) { const s = stroke; stroke = null; this.onPaint && this.onPaint(s); return; }
      if (this._rect) {
        const [a, b] = this._rect; this._rect = null; this.draw();
        if (Math.abs(a[0] - b[0]) > 5 && Math.abs(a[1] - b[1]) > 5 && this.onSelectRect) {
          const p = this._norm(Math.min(a[0], b[0]), Math.min(a[1], b[1])), q = this._norm(Math.max(a[0], b[0]), Math.max(a[1], b[1]));
          this.onSelectRect([Math.max(0, p[0]), Math.max(0, p[1]), Math.min(1, q[0]), Math.min(1, q[1])]);
        }
        return;
      }
      if (dragging && this._down && this.onClick && last && Math.hypot(last[0] - this._down[0], last[1] - this._down[1]) < 4) {
        const n = this._norm(last[0], last[1]); this.onClick(n[0], n[1]);
      }
      dragging = false; el.classList.remove("dragging");
    };
    el.addEventListener("pointerup", end);
    el.addEventListener("pointercancel", end);
    el.addEventListener("pointerleave", () => { this._hover = null; if (this.tool !== "pan") this.draw(); });
    el.addEventListener("dblclick", () => (this.fit ? this.actualSize() : this.fitView()));
  }
  _norm(x, y) { const [ix, iy] = this.toImage(x, y); return [ix / this.img.naturalWidth, iy / this.img.naturalHeight]; }
  _drawStroke() { /* live feedback is the cursor circle; the mask itself is rendered by the server */ }
}

export function loadImage(src) {
  return new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = src; });
}
export function svgEl(tag, attrs = {}) {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
}
export function svgText(x, y, text, attrs = {}) {
  const t = svgEl("text", { x, y, fill: "var(--fg)", "font-size": 13, "paint-order": "stroke", stroke: "rgba(0,0,0,.8)", "stroke-width": 3, ...attrs });
  t.textContent = text;
  return t;
}
