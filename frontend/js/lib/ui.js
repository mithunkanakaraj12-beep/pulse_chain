// Small DOM + formatting helpers. Everything is built with createElement/textContent (never innerHTML), so
// data from the API cannot inject markup.
export const SVGNS = "http://www.w3.org/2000/svg";

export function h(tag, attrs, ...kids) {
  const svg = SVG_TAGS.has(tag);
  const el = svg ? document.createElementNS(SVGNS, tag) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.setAttribute("class", v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2).toLowerCase(), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "value" && !svg) el.value = v;
    else if (k === "checked" || k === "disabled" || k === "hidden") { if (v) el.setAttribute(k, ""); }
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, kids);
  return el;
}
const SVG_TAGS = new Set(["svg", "g", "path", "line", "rect", "circle", "text", "polygon", "polyline", "defs", "marker", "title", "ellipse", "clipPath", "linearGradient", "stop", "use"]);
export function append(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k == null || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}
export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }
export const $ = (sel, root = document) => root.querySelector(sel);

// ---------- formatting ----------
export const fmt = {
  int: (n) => (n == null ? "-" : Math.round(n).toLocaleString("en-US")),
  num: (n, d = 1) => (n == null ? "-" : Number(n).toLocaleString("en-US", { maximumFractionDigits: d, minimumFractionDigits: 0 })),
  pct: (n, d = 0) => (n == null ? "-" : `${n > 0 ? "+" : ""}${Number(n).toFixed(d)}%`),
  days: (n) => (n == null ? "No stockout in view" : n < 1 ? "<1 day" : `${fmt.num(n, 0)} days`),
  date: (s) => {
    if (!s) return "-";
    const d = new Date(`${s}T00:00:00`);
    return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
  },
  dateY: (s) => (s ? new Date(`${s}T00:00:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : "-"),
  dt: (s) => (s ? new Date(s).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "-"),
  title: (s) => (s ? s.charAt(0).toUpperCase() + s.slice(1) : ""),
};
export const RISK_LABEL = { low: "Low", moderate: "Watch", high: "High", critical: "Critical" };
export const RISK_COLOR = { low: "var(--risk-low)", moderate: "var(--risk-moderate)", high: "var(--risk-high)", critical: "var(--risk-critical)" };
export const RISK_HEX = { low: "#0f8b8d", moderate: "#c99a00", high: "#e5731a", critical: "#c62f2f" };
export const RISK_ORDER = { low: 0, moderate: 1, high: 2, critical: 3 };
export const riskChip = (r) => h("span", { class: `chip ${r}` }, RISK_LABEL[r] || r);

// ---------- icons (inline, 18px, currentColor) ----------
const ICONS = {
  dashboard: "M3 3h8v8H3zM13 3h8v5h-8zM13 10h8v11h-8zM3 13h8v8H3z",
  clinics: "M12 2l9 5v2H3V7zM5 11h3v8H5zM10.5 11h3v8h-3zM16 11h3v8h-3zM3 21h18v1H3z",
  forecast: "M3 17l5-6 4 3 5-8 4 4M3 21h18",
  inventory: "M4 7l8-4 8 4v10l-8 4-8-4zM4 7l8 4 8-4M12 11v10",
  transfer: "M4 8h13l-3-3M20 16H7l3 3M4 8l3 3M20 16l-3-3",
  alert: "M12 3l10 18H2zM12 10v5M12 18v.5",
  demo: "M5 4l14 8-14 8z",
  model: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  menu: "M3 6h18M3 12h18M3 18h18",
  refresh: "M20 11a8 8 0 10-2.3 5.7M20 4v7h-7",
};
export function icon(name, size = 18) {
  return h("svg", { viewBox: "0 0 24 24", width: size, height: size, fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" },
    h("path", { d: ICONS[name] || "" }));
}

// ---------- stock runway: usable stock expressed in days against the resupply lead time ----------
export function runway(row, scaleDays = 60) {
  const s = Math.max(scaleDays, (row.lead_time_days || 0) * 1.6, 1);
  const pos = (d) => `${Math.max(0, Math.min(100, ((d ?? s) / s) * 100))}%`;
  const dts = row.days_to_stockout;
  const el = h("div", { class: `runway ${row.risk}`, role: "img",
    "aria-label": `${dts == null ? "No stockout projected within the forecast view" : `Stock lasts about ${fmt.num(dts, 0)} days`}; resupply takes ${row.lead_time_days} days; risk ${row.risk}.` },
    h("div", { class: "track" }));
  if (row.days_to_stockout_pessimistic != null || row.days_to_stockout_optimistic != null) {
    const lo = row.days_to_stockout_pessimistic ?? dts ?? s, hi = row.days_to_stockout_optimistic ?? s;
    el.append(h("div", { class: "band", style: { left: pos(lo), width: `calc(${pos(hi)} - ${pos(lo)})` } }));
  }
  el.append(h("div", { class: "fill", style: { width: pos(dts) } }), h("div", { class: "lead", style: { left: pos(row.lead_time_days) }, title: `Resupply lead time: ${row.lead_time_days} days` }));
  return el;
}
export function runwayKey() {
  return h("div", { class: "runway-key" }, h("span", {}, h("i"), "Days of usable stock"), h("span", {}, h("i", { class: "band" }), "Range if demand is high or low"), h("span", {}, h("i", { class: "lead" }), "Resupply lead time"));
}

// ---------- toast, dialogs, loading, errors ----------
export function toast(msg, kind = "") {
  let box = document.getElementById("toasts");
  if (!box) { box = h("div", { id: "toasts", class: "toasts", role: "status", "aria-live": "polite" }); document.body.append(box); }
  const t = h("div", { class: `toast ${kind}` }, msg);
  box.append(t);
  setTimeout(() => t.remove(), kind === "error" ? 7000 : 4200);
}
export function confirmDialog({ title, body, confirmLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    const dlg = h("dialog", { "aria-labelledby": "dlg-t" });
    const done = (v) => { dlg.close(); dlg.remove(); resolve(v); };
    dlg.append(h("div", { class: "dh" }, h("h2", { id: "dlg-t" }, title)), h("div", { class: "db" }, body),
      h("div", { class: "df" }, h("button", { class: "btn", onClick: () => done(false) }, "Cancel"),
        h("button", { class: `btn ${danger ? "danger" : "primary"}`, onClick: () => done(true) }, confirmLabel)));
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); done(false); });
    document.body.append(dlg); dlg.showModal();
  });
}
export const loading = () => h("div", { class: "grid cols-2" }, h("div", { class: "skeleton" }), h("div", { class: "skeleton" }));
export function errorBox(err, retry) {
  return h("div", { class: "err-box", role: "alert" }, h("b", {}, "This view could not load. "), String(err && err.message ? err.message : err),
    retry ? h("div", { style: { marginTop: "8px" } }, h("button", { class: "btn small", onClick: retry }, "Try again")) : null);
}
export function empty(title, text) { return h("div", { class: "empty" }, h("b", {}, title), text); }

export function pageHeader(title, sub, actions) {
  return h("header", { class: "page" }, h("div", {}, h("h1", {}, title), sub ? h("p", { class: "page-sub" }, sub) : null), actions ? h("div", { class: "row" }, actions) : null);
}
export function panel(title, body, { hint, actions, cls = "" } = {}) {
  return h("section", { class: `panel ${cls}` },
    title ? h("div", { class: "ph" }, h("div", {}, h("h2", {}, title), hint ? h("p", { class: "hint" }, hint) : null), actions || null) : null,
    h("div", { class: "pb" }, body));
}
export function kpi(value, label, { unit, detail, tone = "" } = {}) {
  return h("div", { class: `kpi ${tone}` }, h("div", { class: "v" }, value, unit ? h("small", {}, unit) : null), h("div", { class: "l" }, label), detail ? h("div", { class: "d" }, detail) : null);
}
export function seg(options, value, onChange, label) {
  const g = h("div", { class: "seg", role: "group", "aria-label": label });
  options.forEach(([v, text]) => g.append(h("button", { type: "button", "aria-pressed": String(v === value), onClick: () => {
    [...g.children].forEach((b) => b.setAttribute("aria-pressed", "false")); g.children[options.findIndex((o) => o[0] === v)].setAttribute("aria-pressed", "true"); onChange(v);
  } }, text)));
  return g;
}
export function debounce(fn, ms = 250) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }
