// Offline schematic map. There are no map tiles (they would not load offline); clinics are drawn at their
// real simulated coordinates on a distance-preserving projection, with region outlines and transfer arrows.
import { h, RISK_HEX, RISK_LABEL, fmt } from "./ui.js";
import { showTip, hideTip } from "./charts.js";

const REGION_FILL = { highland: "#c9dcc9", riverine: "#c6dcf0", coastal: "#f0e1c3" };
function hull(points) { // Andrew's monotone chain
  const p = [...points].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (p.length < 3) return p;
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lo = [], up = [];
  for (const q of p) { while (lo.length >= 2 && cross(lo[lo.length - 2], lo[lo.length - 1], q) <= 0) lo.pop(); lo.push(q); }
  for (const q of [...p].reverse()) { while (up.length >= 2 && cross(up[up.length - 2], up[up.length - 1], q) <= 0) up.pop(); up.push(q); }
  return lo.slice(0, -1).concat(up.slice(0, -1));
}

export function renderMap({ points, transfers = [], selectedId = null, activeTransferId = null, onSelect, regionNames = {}, width = 760, height = 470, showSurplus = true }) {
  const lat0 = points.reduce((a, p) => a + p.lat, 0) / points.length, k = Math.cos((lat0 * Math.PI) / 180);
  const xs = points.map((p) => p.lon * k), ys = points.map((p) => -p.lat);
  const pad = 56, minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys);
  const sc = Math.min((width - 2 * pad) / (maxx - minx || 1), (height - 2 * pad) / (maxy - miny || 1));
  const ox = (width - (maxx - minx) * sc) / 2, oy = (height - (maxy - miny) * sc) / 2;
  const P = new Map(points.map((p, i) => [p.id, [ox + (xs[i] - minx) * sc, oy + (ys[i] - miny) * sc, p]]));

  const svg = h("svg", { viewBox: `0 0 ${width} ${height}`, role: "group", "aria-label": "Schematic map of clinics. Use Tab to move between clinics." },
    h("defs", {},
      h("marker", { id: "arrow", viewBox: "0 0 10 10", refX: 8, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, h("path", { d: "M0,0 L10,5 L0,10 z", fill: "#0a2342" })),
      h("marker", { id: "arrow-on", viewBox: "0 0 10 10", refX: 8, refY: 5, markerWidth: 8, markerHeight: 8, orient: "auto-start-reverse" }, h("path", { d: "M0,0 L10,5 L0,10 z", fill: "#0f8b8d" }))));
  for (let gx = 0; gx <= width; gx += 80) svg.append(h("line", { x1: gx, x2: gx, y1: 0, y2: height, stroke: "#d3e0ee", "stroke-width": 1 }));
  for (let gy = 0; gy <= height; gy += 80) svg.append(h("line", { x1: 0, x2: width, y1: gy, y2: gy, stroke: "#d3e0ee", "stroke-width": 1 }));

  const regions = {};
  for (const [x, y, p] of P.values()) (regions[p.region] ||= []).push([x, y]);
  for (const [r, pts] of Object.entries(regions)) {
    const hp = hull(pts);
    const cx = pts.reduce((a, q) => a + q[0], 0) / pts.length, cy = pts.reduce((a, q) => a + q[1], 0) / pts.length;
    if (hp.length >= 3) svg.append(h("path", { d: hp.map((q, i) => `${i ? "L" : "M"}${q[0]},${q[1]}`).join("") + "Z", fill: REGION_FILL[r] || "#ddd", stroke: REGION_FILL[r] || "#ddd", "stroke-width": 46, "stroke-linejoin": "round", opacity: 0.55 }));
    else svg.append(h("circle", { cx, cy, r: 50, fill: REGION_FILL[r] || "#ddd", opacity: 0.55 }));
    svg.append(h("text", { x: cx, y: Math.min(...pts.map((q) => q[1])) - 34, "text-anchor": "middle", style: { fill: "#46607d", fontWeight: 650, fontSize: "12.5px" } }, regionNames[r] || r));
  }

  // transfer arrows (under nodes)
  transfers.forEach((t) => {
    const a = P.get(t.donor_id), b = P.get(t.receiver_id);
    if (!a || !b) return;
    const mx = (a[0] + b[0]) / 2, my = (a[1] + b[1]) / 2, dx = b[0] - a[0], dy = b[1] - a[1], len = Math.hypot(dx, dy) || 1;
    const bend = 0.16 * len, cx = mx - (dy / len) * bend, cy = my + (dx / len) * bend;
    const on = activeTransferId === t.id, r2 = 13;
    const ex = b[0] - ((b[0] - cx) / (Math.hypot(b[0] - cx, b[1] - cy) || 1)) * r2, ey = b[1] - ((b[1] - cy) / (Math.hypot(b[0] - cx, b[1] - cy) || 1)) * r2;
    const path = h("path", { d: `M${a[0]},${a[1]} Q${cx},${cy} ${ex},${ey}`, fill: "none", stroke: on ? "#0f8b8d" : "#0a2342", "stroke-width": on ? 3.4 : 1.7, opacity: on ? 1 : activeTransferId ? 0.18 : 0.6, "marker-end": `url(#${on ? "arrow-on" : "arrow"})`, "stroke-dasharray": on ? "" : "6 4" });
    path.append(h("title", {}, `${t.medicine_name}: ${t.donor_name} to ${t.receiver_name}, ${t.quantity} ${t.unit}`));
    svg.append(path);
  });

  for (const [x, y, p] of P.values()) {
    const r = 9 + Math.sqrt(p.catchment_pop / 1000) * 1.9, sel = p.id === selectedId;
    const g = h("g", { class: "map-node", tabindex: 0, role: "button", "aria-label": `${p.name}, ${RISK_LABEL[p.worst_risk]} risk${p.surplus_units > 0 ? `, ${p.surplus_units} units available to share` : ""}`, transform: `translate(${x},${y})` });
    if (p.worst_risk === "critical" || p.worst_risk === "high") g.append(h("circle", { r: r + 7, fill: RISK_HEX[p.worst_risk], opacity: 0.18 }));
    g.append(h("circle", { class: "ring", r: r + 3, fill: "#fff", stroke: sel ? "#0a2342" : "#fff", "stroke-width": sel ? 3 : 1 }),
      h("circle", { r, fill: RISK_HEX[p.worst_risk] }));
    if (showSurplus && p.surplus_units > 0) g.append(h("circle", { cx: r * 0.8, cy: -r * 0.8, r: 5.5, fill: "#fff", stroke: "#0f8b8d", "stroke-width": 2 }), h("text", { x: r * 0.8, y: -r * 0.8 + 3, "text-anchor": "middle", style: { fontSize: "8px", fontWeight: 800, fill: "#0f8b8d" } }, "+"));
    g.append(h("text", { y: r + 16, "text-anchor": "middle", style: { fontSize: "11px", fontWeight: 650, fill: "#0a2342", paintOrder: "stroke", stroke: "#fff", strokeWidth: "3px" } }, p.id));
    g.addEventListener("mousemove", (e) => showTip(e, p.name, [`${RISK_LABEL[p.worst_risk]} risk`, `Serves ${fmt.int(p.catchment_pop)} people`, p.surplus_units > 0 ? `${p.surplus_units} units could be shared` : "No spare stock to share"]));
    g.addEventListener("mouseleave", hideTip);
    const pick = () => onSelect && onSelect(p.id);
    g.addEventListener("click", pick); g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick(); } });
    svg.append(g);
  }
  const wrap = h("div", { class: "map-wrap" }, svg,
    h("div", { class: "map-note" }, "Schematic map, simulated locations"),
    h("div", { class: "map-legend" }, ["critical", "high", "moderate", "low"].map((r) => h("div", {}, h("i", { style: { background: RISK_HEX[r] } }), `${RISK_LABEL[r]} risk`)),
      h("div", {}, h("i", { style: { background: "#fff", border: "2px solid #0f8b8d" } }), "Has stock to share"), h("div", {}, "Arrow: proposed transfer")));
  return wrap;
}
