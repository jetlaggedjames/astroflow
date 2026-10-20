// Lightweight canvas charts: quality graph with threshold, histogram, sparkline, altitude curve.
const css = (n) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

function setup(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const r = canvas.getBoundingClientRect();
  const W = Math.max(10, r.width), H = Math.max(10, r.height);
  if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  return { ctx, W, H };
}

// Quality per frame (bars or line), a draggable threshold, highlighted kept frames and flagged frames.
export class QualityChart {
  constructor(canvas, { onThreshold, onPick } = {}) {
    this.c = canvas; this.data = []; this.kept = new Set(); this.flags = []; this.threshold = null;
    this.onThreshold = onThreshold; this.onPick = onPick; this.metric = "quality"; this.series = null;
    let drag = false;
    canvas.addEventListener("pointerdown", (e) => {
      const { y, x } = this._pos(e);
      if (this.threshold !== null && Math.abs(y - this._ty(this.threshold)) < 10) { drag = true; canvas.setPointerCapture(e.pointerId); return; }
      if (this.onPick && this.data.length) this.onPick(Math.min(this.data.length - 1, Math.max(0, Math.round(x / this._W * (this.data.length - 1)))));
    });
    canvas.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const { y } = this._pos(e);
      this.threshold = Math.max(0, Math.min(this._max, (1 - (y - 8) / (this._H - 16)) * this._max));
      this.draw();
    });
    canvas.addEventListener("pointerup", () => { if (drag) { drag = false; this.onThreshold && this.onThreshold(this.threshold); } });
    new ResizeObserver(() => this.draw()).observe(canvas);
  }
  _pos(e) { const r = this.c.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; }
  _ty(v) { return 8 + (1 - v / (this._max || 1)) * (this._H - 16); }
  set(values, { kept, flags, threshold, label } = {}) {
    this.data = values || []; if (kept) this.kept = kept; if (flags) this.flags = flags;
    if (threshold !== undefined) this.threshold = threshold; this.label = label; this.draw();
  }
  draw() {
    const { ctx, W, H } = setup(this.c);
    this._W = W; this._H = H;
    const v = this.data.filter((x) => x !== null && isFinite(x));
    this._max = v.length ? Math.max(...v) * 1.05 || 1 : 1;
    ctx.fillStyle = css("--fg3"); ctx.font = "11px sans-serif";
    if (!this.data.length) { ctx.fillText("No data yet", 10, 20); return; }
    const n = this.data.length;
    const bw = Math.max(1, W / n);
    const acc = css("--accent"), dim = css("--fg3"), bad = css("--danger");
    for (let i = 0; i < n; i++) {
      const val = this.data[i];
      if (val === null || !isFinite(val)) continue;
      const x = (i / n) * W, y = this._ty(val);
      ctx.fillStyle = this.flags[i] && this.flags[i].length ? bad : this.kept.has(i) ? acc : dim;
      ctx.fillRect(x, y, Math.max(1, bw - (bw > 3 ? 1 : 0)), H - 8 - y);
    }
    if (this.threshold !== null && this.threshold !== undefined) {
      const y = this._ty(this.threshold);
      ctx.strokeStyle = css("--warn"); ctx.lineWidth = 2; ctx.setLineDash([6, 4]);
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke(); ctx.setLineDash([]);
      ctx.fillStyle = css("--warn"); ctx.fillText("drag to set the keep threshold", 6, y - 4);
    }
    if (this.label) { ctx.fillStyle = css("--fg2"); ctx.fillText(this.label, W - ctx.measureText(this.label).width - 6, 14); }
  }
}

export function drawHistogram(canvas, hist, { log = false, markers = [] } = {}) {
  const { ctx, W, H } = setup(canvas);
  if (!hist) return;
  const chans = Object.entries(hist).filter(([k, v]) => Array.isArray(v));
  const colors = { R: "rgba(255,80,80,.75)", G: "rgba(80,220,80,.75)", B: "rgba(90,140,255,.8)", L: css("--fg2") };
  let max = 1;
  for (const [, arr] of chans) for (let i = 1; i < arr.length - 1; i++) max = Math.max(max, log ? Math.log1p(arr[i]) : arr[i]);
  ctx.globalCompositeOperation = document.documentElement.dataset.theme === "day" ? "multiply" : "screen";
  for (const [k, arr] of chans) {
    ctx.fillStyle = colors[k] || css("--fg2");
    ctx.beginPath(); ctx.moveTo(0, H);
    for (let i = 0; i < arr.length; i++) {
      const v = log ? Math.log1p(arr[i]) : arr[i];
      ctx.lineTo((i / (arr.length - 1)) * W, H - Math.min(1, v / max) * (H - 4));
    }
    ctx.lineTo(W, H); ctx.closePath(); ctx.fill();
  }
  ctx.globalCompositeOperation = "source-over";
  for (const m of markers) { ctx.strokeStyle = m.color || css("--warn"); ctx.beginPath(); ctx.moveTo(m.x * W, 0); ctx.lineTo(m.x * W, H); ctx.stroke(); }
}

export function drawSparkline(canvas, values, { label, better = "higher" } = {}) {
  const { ctx, W, H } = setup(canvas);
  const v = values.filter((x) => x !== null && x !== undefined && isFinite(x));
  if (v.length < 2) { ctx.fillStyle = css("--fg3"); ctx.font = "11px sans-serif"; ctx.fillText("Waiting for frames...", 8, 18); return; }
  const lo = Math.min(...v), hi = Math.max(...v), span = hi - lo || 1;
  ctx.strokeStyle = css("--accent"); ctx.lineWidth = 2; ctx.beginPath();
  v.forEach((x, i) => { const px = (i / (v.length - 1)) * (W - 4) + 2, py = H - 6 - ((x - lo) / span) * (H - 22); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
  ctx.stroke();
  const best = better === "higher" ? hi : lo;
  ctx.fillStyle = css("--fg2"); ctx.font = "11px sans-serif";
  ctx.fillText(`${label || ""} now ${v[v.length - 1].toFixed(2)}  best ${best.toFixed(2)}`, 6, 12);
}

export function drawAltitude(canvas, alts, { limit = 25, times = [] } = {}) {
  const { ctx, W, H } = setup(canvas);
  if (!alts || !alts.length) return;
  const y = (a) => H - 2 - (Math.max(0, a) / 90) * (H - 4);
  ctx.fillStyle = css("--bg4"); ctx.fillRect(0, y(limit), W, H - y(limit));
  ctx.strokeStyle = css("--accent"); ctx.lineWidth = 1.5; ctx.beginPath();
  alts.forEach((a, i) => { const px = (i / (alts.length - 1)) * W; i ? ctx.lineTo(px, y(a)) : ctx.moveTo(px, y(a)); });
  ctx.stroke();
}
