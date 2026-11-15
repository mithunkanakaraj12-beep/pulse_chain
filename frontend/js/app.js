// App shell: hash router, navigation, connectivity indicator, scenario and offline banners.
import { h, clear, $, icon, toast, errorBox, loading, fmt, confirmDialog } from "./lib/ui.js";
import * as api from "./lib/api.js";

const ROUTES = [
  { path: "dashboard", label: "Dashboard", icon: "dashboard", load: () => import("./pages/dashboard.js") },
  { path: "clinics", label: "Clinics", icon: "clinics", load: () => import("./pages/clinics.js") },
  { path: "forecast", label: "Forecasting Center", icon: "forecast", load: () => import("./pages/forecast.js") },
  { path: "inventory", label: "Inventory", icon: "inventory", load: () => import("./pages/inventory.js") },
  { path: "redistribution", label: "Redistribution", icon: "transfer", load: () => import("./pages/redistribution.js") },
  { path: "alerts", label: "Alerts Center", icon: "alert", load: () => import("./pages/alerts.js"), badge: true },
  { path: "demo", label: "Monsoon Demo", icon: "demo", load: () => import("./pages/demo.js") },
  { path: "model", label: "Model & Data", icon: "model", load: () => import("./pages/model.js") },
];
const state = { meta: null, alertsActive: 0, current: null, cleanup: null, token: 0 };
export const app = { get meta() { return state.meta; }, refreshMeta, rerender, navigate };

function parseHash() {
  const [path, ...rest] = (location.hash.replace(/^#\/?/, "") || "dashboard").split("/");
  return { path: path || "dashboard", params: rest.map(decodeURIComponent), query: new URLSearchParams(location.hash.split("?")[1] || "") };
}
export function navigate(to) { location.hash = `#/${to}`; }

async function refreshMeta() {
  state.meta = await api.get("/api/meta");
  try { state.alertsActive = (await api.get("/api/alerts")).active; } catch { /* keep last */ }
  drawChrome();
  return state.meta;
}

function drawChrome() {
  const cur = parseHash().path;
  const nav = clear($("#nav"));
  ROUTES.forEach((r) => nav.append(h("a", { href: `#/${r.path}`, "aria-current": r.path === cur ? "page" : null }, icon(r.icon), r.label,
    r.badge && state.alertsActive ? h("span", { class: "count" }, state.alertsActive) : null)));
  const foot = clear($("#railfoot"));
  foot.append(h("div", {}, "Demo clock: ", h("b", {}, state.meta ? fmt.dateY(state.meta.demo_today) : "-")), h("div", {}, "SMS: ", h("b", {}, state.meta ? state.meta.sms.mode : "-")),
    h("button", { class: "btn small onnavy", onClick: resetDemo }, icon("refresh", 14), "Reset demo data"));
  drawTopbar(); drawBanners();
}

function drawTopbar() {
  const bar = clear($("#topbar")), q = api.queueLength();
  bar.append(h("button", { class: "btn small menu-btn", "aria-label": "Open navigation", onClick: () => document.body.classList.toggle("nav-open") }, icon("menu")),
    h("div", { class: "simlabel" }, h("b", {}, "Simulated data"), h("span", {}, "Illustrative demo dataset. Not real epidemiological or inventory data.")),
    h("span", { class: "spacer" }),
    h("span", { class: `net ${api.net.online ? "" : "offline"}`, role: "status" }, h("i"), api.net.online ? "Online" : "Offline",
      q ? h("span", { class: "q" }, `${q} edit${q > 1 ? "s" : ""} waiting to sync`) : (!api.net.online ? h("span", { class: "q" }, "showing saved data") : null)));
}

function drawBanners() {
  const box = clear($("#banners"));
  if (!api.net.online) {
    const q = api.queueLength();
    box.append(h("div", { class: "banner offline", role: "status" }, h("b", {}, "You are offline."), "Dashboards show the last data saved on this device.",
      q ? `${q} edit${q > 1 ? "s are" : " is"} saved here and will sync when you reconnect.` : "Edits to inventory, alerts and transfers are saved here and sync on reconnect.", "Forecasts cannot be recalculated offline."));
  }
  const sc = state.meta && state.meta.scenario;
  if (sc && sc.monsoon) {
    box.append(h("div", { class: "banner scenario", role: "status" }, h("b", {}, "Simulated monsoon is applied."),
      `Rain is raised ${sc.intensity}x for ${sc.weeks} week${sc.weeks > 1 ? "s" : ""} in ${sc.regions.length} region${sc.regions.length > 1 ? "s" : ""}. Every number you see is a what-if on synthetic data.`,
      h("button", { class: "btn small", onClick: async () => { await api.post("/api/scenario", { monsoon: false }); await refreshMeta(); rerender(); toast("Back to normal conditions."); } }, "Return to normal")));
  }
}

async function resetDemo() {
  const ok = await confirmDialog({ title: "Reset the demo?", body: "This regenerates the simulated data, clears approved transfers, the SMS log and any monsoon scenario. Edits waiting to sync on this device are discarded.", confirmLabel: "Reset demo", danger: true });
  if (!ok) return;
  try { api.discardQueue(); await api.post("/api/admin/reset", {}); await refreshMeta(); rerender(); toast("Demo data reset."); } catch (e) { toast(e.message, "error"); }
}

let pending = null;
export function rerender() { return route(); }
async function route() {
  const token = ++state.token, { path, params, query } = parseHash();
  const def = ROUTES.find((r) => r.path === path) || ROUTES[0];
  document.body.classList.remove("nav-open");
  document.title = `${def.label} - PulseChain AI`;
  if (state.cleanup) { try { state.cleanup(); } catch { /* ignore */ } state.cleanup = null; }
  drawChrome();
  const view = $("#view");
  const keepScroll = state.current === location.hash;
  const y = keepScroll ? window.scrollY : 0;
  if (!keepScroll) clear(view).append(loading());
  try {
    if (!state.meta) await refreshMeta();
    const mod = await def.load();
    const out = await mod.render({ params, query, meta: state.meta, app });
    if (token !== state.token) return; // a newer navigation won
    clear(view).append(out.node || out);
    state.cleanup = out.cleanup || null;
    state.current = location.hash;
    if (!keepScroll) { view.focus({ preventScroll: true }); window.scrollTo(0, 0); } else window.scrollTo(0, y);
  } catch (e) {
    if (token !== state.token) return;
    clear(view).append(errorBox(e, () => route()));
  }
}

function boot() {
  window.addEventListener("hashchange", () => { state.current = null; route(); });
  api.bus.addEventListener("net", () => { drawChrome(); if (api.net.online) route(); });
  api.bus.addEventListener("queue", drawTopbar);
  api.bus.addEventListener("queue", () => drawBanners());
  api.bus.addEventListener("synced", async (e) => {
    const { sent, rejected } = e.detail;
    if (sent) toast(`Back online. ${sent} saved edit${sent > 1 ? "s" : ""} synced.`);
    rejected.forEach((r) => toast(`${r.label} was not applied: ${r.reason}`, "warn"));
    await refreshMeta().catch(() => {}); route();
  });
  api.bus.addEventListener("mutated", () => refreshMeta().catch(() => {}));
  if ("serviceWorker" in navigator) {
    navigator.serviceWorker.register("/sw.js").catch(() => { /* offline mode unavailable (e.g. insecure origin); app still works online */ });
  }
  route();
  if (api.queueLength() && api.net.online) api.flushQueue();
}
boot();
