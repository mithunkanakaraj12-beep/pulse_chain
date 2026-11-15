// Dependency-free SVG charts. Each function returns an <svg> element. Colours come from CSS tokens so charts
// follow the theme; shapes are paired with direct labels so colour is never the only cue.
import { h, fmt } from "./ui.js";

const DEFAULT_W = 640;
function scale(domain, range) {
  const [d0, d1] = domain, [r0, r1] = range, span = d1 - d0 || 1;
  return (v) => r0 + ((v - d0) / span) * (r1 - r0);
}
export function niceMax(v) {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v)), f = v / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}
const pathOf = (pts) => pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");

let tipEl = null;
export function showTip(evt, title, lines) {
  if (!tipEl) { tipEl = h("div", { class: "tip", role: "tooltip" }); document.body.append(tipEl); }
  tipEl.replaceChildren(h("b", {}, title), ...lines.map((l) => h("div", {}, l)));
  tipEl.style.display = "block";
  const x = Math.min(window.innerWidth - 270, evt.clientX + 14), y = evt.clientY + 14;
  tipEl.style.left = `${Math.max(8, x)}px`; tipEl.style.top = `${y}px`;
}
export function hideTip() { if (tipEl) tipEl.style.display = "none"; }

/**
 * Time-series chart: observed history, forecast with uncertainty band, optional comparison series.
 * opts: {height, history:[{x:label,y}], forecast:{x:[labels],expected,lower,upper}, lines:[{name,color,dash,y:[...]}] (aligned to forecast x),
 *        secondary:{name,y:[...history values], color} (e.g., rainfall bars on history), unit, ariaLabel, markAt }
 */
function forecastSvg(o) {
  const W = o.width || DEFAULT_W;
  const H = o.height || 280, m = { l: 46, r: 14, t: 14, b: 30 };
  const hist = o.history || [], f = o.forecast;
  const labels = [...hist.map((p) => p.x), ...f.x];
  const n = labels.length;
  const ys = [...hist.map((p) => p.y), ...f.upper, ...f.expected, ...(o.lines || []).flatMap((l) => l.y)];
  const ymax = niceMax(Math.max(...ys.filter((v) => v != null), 1) * 1.05);
  const sx = scale([0, n - 1], [m.l, W - m.r]), sy = scale([0, ymax], [H - m.b, m.t]);
  const svg = h("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.ariaLabel || "Time series chart" });
  for (let i = 0; i <= 4; i++) {
    const v = (ymax / 4) * i, y = sy(v);
    svg.append(h("line", { class: "grid-line", x1: m.l, x2: W - m.r, y1: y, y2: y }), h("text", { x: m.l - 7, y: y + 4, "text-anchor": "end" }, fmt.num(v, ymax < 10 ? 1 : 0)));
  }
  const tickEvery = Math.ceil(n / 8);
  labels.forEach((l, i) => { if (i % tickEvery === 0 || i === n - 1) svg.append(h("text", { x: sx(i), y: H - 9, "text-anchor": "middle" }, fmt.date(l))); });
  svg.append(h("line", { class: "axis", x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }));
  const hn = hist.length;
  if (hn) { // divider: where observation ends and the forecast begins
    const x = sx(hn - 0.5);
    svg.append(h("line", { x1: x, x2: x, y1: m.t, y2: H - m.b, stroke: "var(--ink-3)", "stroke-dasharray": "3 4" }), h("text", { x: x + 5, y: m.t + 9 }, "forecast >"));
  }
  const fx = (i) => sx(hn + i);
  // band
  const up = f.upper.map((v, i) => [fx(i), sy(v)]), lo = f.lower.map((v, i) => [fx(i), sy(v)]).reverse();
  const bridge = hn ? [[sx(hn - 1), sy(hist[hn - 1].y)]] : [];
  svg.append(h("path", { d: pathOf([...bridge, ...up, ...lo, ...bridge]) + "Z", fill: "var(--blue)", opacity: ".16" }));
  (o.lines || []).forEach((l) => svg.append(h("path", { d: pathOf(l.y.map((v, i) => [fx(i), sy(v)])), fill: "none", stroke: l.color, "stroke-width": 1.6, "stroke-dasharray": l.dash || "", opacity: ".95" })));
  if (hn) svg.append(h("path", { d: pathOf(hist.map((p, i) => [sx(i), sy(p.y)])), fill: "none", stroke: "var(--ink)", "stroke-width": 1.8, "stroke-linejoin": "round" }));
  svg.append(h("path", { d: pathOf([...bridge, ...f.expected.map((v, i) => [fx(i), sy(v)])]), fill: "none", stroke: o.color || "var(--blue)", "stroke-width": 2.6, "stroke-linejoin": "round" }));
  f.expected.forEach((v, i) => svg.append(h("circle", { cx: fx(i), cy: sy(v), r: 3, fill: o.color || "var(--blue)" })));
  // hover layer
  const hit = h("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" });
  const cursor = h("line", { y1: m.t, y2: H - m.b, stroke: "var(--ink-3)", opacity: 0 });
  svg.append(cursor, hit);
  hit.addEventListener("mousemove", (e) => {
    const r = svg.getBoundingClientRect(), px = ((e.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / (W - m.l - m.r)) * (n - 1))));
    cursor.setAttribute("x1", sx(i)); cursor.setAttribute("x2", sx(i)); cursor.setAttribute("opacity", 1);
    const unit = o.unit || "cases";
    if (i < hn) showTip(e, fmt.dateY(labels[i]), [`Observed: ${fmt.int(hist[i].y)} ${unit}`]);
    else { const j = i - hn; showTip(e, `${fmt.dateY(labels[i])} (forecast)`, [`Expected: ${fmt.num(f.expected[j], 0)} ${unit}`, `80% range: ${fmt.num(f.lower[j], 0)} to ${fmt.num(f.upper[j], 0)}`, ...(o.lines || []).map((l) => `${l.name}: ${fmt.num(l.y[j], 0)}`)]); }
  });
  hit.addEventListener("mouseleave", () => { hideTip(); cursor.setAttribute("opacity", 0); });
  return svg;
}

export function chartLegend(items) {
  return h("div", { class: "legend" }, items.map((i) => h("span", { style: i.color ? { color: i.color } : {} }, h("i", { class: i.kind || "", style: { background: i.kind === "dash" ? "" : i.color } }), h("span", { style: { color: "var(--ink-2)" } }, i.label))));
}

/** Grouped/simple vertical bars. data: [{label, values:[{v,color,name}]}] */
function barSvg({ data, height = 220, unit = "", ariaLabel, yMax, format = (v) => fmt.num(v, 0), width: W = DEFAULT_W }) {
  const H = height, m = { l: 44, r: 10, t: 14, b: 50 };
  const max = yMax || niceMax(Math.max(...data.flatMap((d) => d.values.map((x) => x.v)), 1) * 1.08);
  const sy = scale([0, max], [H - m.b, m.t]);
  const svg = h("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": ariaLabel || "Bar chart" });
  for (let i = 0; i <= 4; i++) { const v = (max / 4) * i, y = sy(v); svg.append(h("line", { class: "grid-line", x1: m.l, x2: W - m.r, y1: y, y2: y }), h("text", { x: m.l - 7, y: y + 4, "text-anchor": "end" }, format(v))); }
  const slot = (W - m.l - m.r) / data.length;
  data.forEach((d, i) => {
    const k = d.values.length, gw = Math.min(slot * 0.78, 22 * k + 4), bw = gw / k, x0 = m.l + slot * i + (slot - gw) / 2;
    d.values.forEach((s, j) => {
      const y = sy(s.v), r = h("rect", { x: x0 + bw * j, y, width: Math.max(1, bw - 2), height: Math.max(0, H - m.b - y), rx: 3, fill: s.color });
      r.addEventListener("mousemove", (e) => showTip(e, d.label, [`${s.name || ""} ${format(s.v)} ${unit}`.trim()])); r.addEventListener("mouseleave", hideTip);
      svg.append(r);
      if (data.length <= 10 && k === 1) svg.append(h("text", { x: x0 + bw * j + bw / 2, y: y - 5, "text-anchor": "middle", style: { fill: "var(--ink-2)", fontWeight: 600 } }, format(s.v)));
    });
    const per = Math.max(5, Math.floor(slot / 6.4)), lines = [];
    String(d.label).split(" ").forEach((w) => { const last = lines[lines.length - 1]; if (last !== undefined && (last + " " + w).length <= per) lines[lines.length - 1] = last + " " + w; else lines.push(w); });
    lines.slice(0, 3).forEach((ln, k) => svg.append(h("text", { x: m.l + slot * (i + 0.5), y: H - m.b + 15 + k * 12, "text-anchor": "middle" }, ln)));
  });
  svg.append(h("line", { class: "axis", x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }));
  return svg;
}

/** Horizontal stacked distribution bar (e.g., risk mix). parts: [{label,value,color}] */
function stackSvg(parts, { height = 26, width: W = DEFAULT_W } = {}) {
  const total = parts.reduce((a, p) => a + p.value, 0) || 1;
  const svg = h("svg", { class: "chart", viewBox: `0 0 ${W} ${height + 18}`, role: "img", "aria-label": parts.map((p) => `${p.label}: ${p.value}`).join(", ") });
  let x = 0;
  parts.forEach((p) => {
    const w = (p.value / total) * W;
    if (w > 0) {
      svg.append(h("rect", { x, y: 0, width: w, height, fill: p.color, rx: 0 }));
      if (w > 28) svg.append(h("text", { x: x + w / 2, y: height / 2 + 4, "text-anchor": "middle", style: { fill: "#fff", fontWeight: 700, fontSize: "12px" } }, p.value));
    }
    x += w;
  });
  return svg;
}

/** Dual-series line comparison (baseline vs scenario), with band for the scenario. */
function compareSvg({ dates, a, b, height = 240, unit = "cases", nameA = "Normal", nameB = "Monsoon", ariaLabel, width: W = DEFAULT_W }) {
  const H = height, m = { l: 46, r: 14, t: 14, b: 30 }, n = dates.length;
  const ymax = niceMax(Math.max(...a.upper, ...b.upper, 1) * 1.05);
  const sx = scale([0, n - 1], [m.l + 8, W - m.r - 8]), sy = scale([0, ymax], [H - m.b, m.t]);
  const svg = h("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": ariaLabel || "Normal versus monsoon forecast" });
  for (let i = 0; i <= 4; i++) { const v = (ymax / 4) * i, y = sy(v); svg.append(h("line", { class: "grid-line", x1: m.l, x2: W - m.r, y1: y, y2: y }), h("text", { x: m.l - 7, y: y + 4, "text-anchor": "end" }, fmt.num(v, 0))); }
  dates.forEach((d, i) => svg.append(h("text", { x: sx(i), y: H - 9, "text-anchor": "middle" }, fmt.date(d))));
  const band = (s, color) => svg.append(h("path", { d: pathOf(s.upper.map((v, i) => [sx(i), sy(v)])) + pathOf(s.lower.map((v, i) => [sx(i), sy(v)]).reverse()).replace("M", "L") + "Z", fill: color, opacity: ".14" }));
  band(a, "var(--blue)"); band(b, "var(--orange)");
  svg.append(h("path", { d: pathOf(a.expected.map((v, i) => [sx(i), sy(v)])), fill: "none", stroke: "var(--blue)", "stroke-width": 2.6 }),
    h("path", { d: pathOf(b.expected.map((v, i) => [sx(i), sy(v)])), fill: "none", stroke: "var(--orange)", "stroke-width": 2.6 }));
  a.expected.forEach((v, i) => svg.append(h("circle", { cx: sx(i), cy: sy(v), r: 3, fill: "var(--blue)" })));
  b.expected.forEach((v, i) => svg.append(h("circle", { cx: sx(i), cy: sy(v), r: 3, fill: "var(--orange)" })));
  const li = n - 1;
  svg.append(h("text", { x: sx(li), y: sy(b.expected[li]) - 9, "text-anchor": "end", style: { fill: "var(--orange)", fontWeight: 700, fontSize: "12px" } }, nameB),
    h("text", { x: sx(li), y: sy(a.expected[li]) + 17, "text-anchor": "end", style: { fill: "var(--blue)", fontWeight: 700, fontSize: "12px" } }, nameA));
  const hit = h("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" });
  hit.addEventListener("mousemove", (e) => {
    const r = svg.getBoundingClientRect(), px = ((e.clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - m.l - 8) / (W - m.l - m.r - 16)) * (n - 1))));
    showTip(e, `Week of ${fmt.dateY(dates[i])}`, [`${nameA}: ${fmt.num(a.expected[i], 0)} ${unit}`, `${nameB}: ${fmt.num(b.expected[i], 0)} ${unit}`]);
  });
  hit.addEventListener("mouseleave", hideTip);
  svg.append(hit);
  return svg;
}

/** Tiny sparkline from a numeric array. */
export function spark(values, { w = 110, hgt = 28, color = "var(--blue)" } = {}) {
  const max = Math.max(...values, 1), sx = scale([0, values.length - 1], [2, w - 2]), sy = scale([0, max], [hgt - 3, 3]);
  return h("svg", { viewBox: `0 0 ${w} ${hgt}`, width: w, height: hgt, role: "img", "aria-label": `Trend, latest ${fmt.num(values[values.length - 1], 0)}` },
    h("path", { d: pathOf(values.map((v, i) => [sx(i), sy(v)])), fill: "none", stroke: color, "stroke-width": 1.8 }));
}

/** Render a chart at the real pixel width of its container so text stays legible; re-renders on resize. */
export function fit(build, minW = 240) {
  const box = h("div", { class: "fit" });
  let last = 0;
  const draw = () => { const w = Math.round(box.clientWidth); if (w < 40 || w === last) return; last = w; box.replaceChildren(build(Math.max(minW, w))); };
  if (typeof ResizeObserver !== "undefined") new ResizeObserver(draw).observe(box); else requestAnimationFrame(draw);
  return box;
}
export const forecastChart = (o) => fit((w) => forecastSvg({ ...o, width: w }));
export const barChart = (o) => fit((w) => barSvg({ ...o, width: w }));
export const stackBar = (parts, o = {}) => fit((w) => stackSvg(parts, { ...o, width: w }));
export const compareChart = (o) => fit((w) => compareSvg({ ...o, width: w }));
