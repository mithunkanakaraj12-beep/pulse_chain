import { h, fmt, kpi, panel, pageHeader, riskChip, runway, runwayKey, RISK_ORDER, RISK_LABEL, empty, seg, debounce, confirmDialog, toast } from "../lib/ui.js";
import { forecastChart, chartLegend, barChart } from "../lib/charts.js";
import * as api from "../lib/api.js";
import { signalLine } from "./shared.js";

export async function render({ params, app, meta }) {
  if (params[0]) return detail(params[0], app, meta);
  const { clinics } = await api.get("/api/clinics");
  const state = { q: "", region: "all", sort: "risk" };
  const body = h("div", {});
  const regionName = Object.fromEntries(meta.regions.map((r) => [r.id, r.name]));
  const draw = () => {
    let rows = clinics.filter((c) => (state.region === "all" || c.region === state.region) && (`${c.name} ${c.id} ${c.health_worker}`).toLowerCase().includes(state.q.toLowerCase()));
    const worst = (c) => (c.medicines_at_risk ? 2 : 0);
    rows.sort((a, b) => state.sort === "name" ? a.name.localeCompare(b.name) : state.sort === "pop" ? b.catchment_pop - a.catchment_pop : ((a.earliest_stockout_days ?? 999) - (b.earliest_stockout_days ?? 999)));
    body.replaceChildren(rows.length ? h("div", { class: "tablewrap" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, ["Clinic", "Region", "Serves", "Resupply takes", "Medicines at risk", "First stockout", "Health worker"].map((t, i) => h("th", { class: i === 2 || i === 3 ? "r" : "" }, t)))),
      h("tbody", {}, rows.map((c) => h("tr", { class: "clickable", tabindex: 0, onClick: () => app.navigate(`clinics/${c.id}`), onKeydown: (e) => { if (e.key === "Enter") app.navigate(`clinics/${c.id}`); } },
        h("td", {}, h("a", { href: `#/clinics/${c.id}`, onClick: (e) => e.stopPropagation() }, h("b", {}, c.name)), h("div", { class: "faint" }, c.id)),
        h("td", {}, regionName[c.region]), h("td", { class: "r" }, `${fmt.int(c.catchment_pop)} people`), h("td", { class: "r" }, `${c.lead_time_days} days`),
        h("td", {}, c.medicines_at_risk ? h("span", { class: `chip ${c.medicines_at_risk >= 3 ? "critical" : "high"}` }, `${c.medicines_at_risk} of ${c.medicines_total}`) : h("span", { class: "chip low" }, `None of ${c.medicines_total}`)),
        h("td", {}, fmt.days(c.earliest_stockout_days)), h("td", { class: "muted" }, c.health_worker)))))) : empty("No clinics match", "Clear the search or pick another region."));
  };
  const search = h("div", { class: "field" }, h("label", { for: "cq" }, "Search clinics"), h("input", { id: "cq", type: "search", placeholder: "Name, ID or health worker", onInput: debounce((e) => { state.q = e.target.value; draw(); }, 120) }));
  draw();
  return h("div", {}, pageHeader("Clinics", `${clinics.length} clinics across ${meta.regions.length} regions. Open one to see its stock, disease trend and suggested actions.`),
    panel(null, h("div", { class: "stack" }, h("div", { class: "row" }, search,
      h("div", { class: "field" }, h("span", {}, "Region"), seg([["all", "All"], ...meta.regions.map((r) => [r.id, r.name.replace(" District", "")])], "all", (v) => { state.region = v; draw(); }, "Region")),
      h("div", { class: "field" }, h("span", {}, "Sort by"), seg([["risk", "Soonest stockout"], ["pop", "Population"], ["name", "Name"]], "risk", (v) => { state.sort = v; draw(); }, "Sort"))), body)));
}

async function detail(id, app, meta) {
  const c = await api.get(`/api/clinics/${encodeURIComponent(id)}`);
  const cl = c.clinic;
  const inv = [...c.inventory].sort((a, b) => RISK_ORDER[b.risk] - RISK_ORDER[a.risk]);
  const worst = inv[0]?.risk || "low";
  const ribbon = h("div", { class: "ribbon" },
    kpi(fmt.int(cl.catchment_pop), "People served"), kpi(`${cl.lead_time_days}`, "Resupply takes", { unit: "days" }),
    kpi(RISK_LABEL[worst], "Worst stock risk", { tone: worst === "critical" ? "danger" : worst === "high" ? "warn" : worst === "low" ? "good" : "" }),
    kpi(c.recommended_actions.length, "Suggested actions", { detail: c.transfers.length ? `${c.transfers.length} transfer option${c.transfers.length > 1 ? "s" : ""}` : "No transfer options" }),
    kpi(cl.has_cold_chain ? "Yes" : "No", "Cold chain"));
  const trends = c.trends.map((t) => h("div", { class: "panel" }, h("div", { class: "ph" }, h("div", {}, h("h2", {}, `${t.disease_name} at this clinic`), h("p", { class: "hint" }, signalLine(t.signal)))),
    h("div", { class: "pb" }, forecastChart({ height: 220, history: t.history.slice(-26).map((p) => ({ x: p.week_start, y: p.cases })), forecast: { x: t.forecast.map((f) => f.week_start), expected: t.forecast.map((f) => f.expected), lower: t.forecast.map((f) => f.lower), upper: t.forecast.map((f) => f.upper) },
      lines: [{ name: "Seasonal norm", color: "#0f8b8d", dash: "5 4", y: t.forecast.map((f) => f.baseline_seasonal) }], ariaLabel: `${t.disease_name} cases at ${cl.name}` }))));
  const invTable = h("div", { class: "tablewrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Medicine"), h("th", { class: "r" }, "Usable"), h("th", {}, "Stock runway"), h("th", {}, "Risk"), h("th", {}, "Order by"), h("th", { class: "r" }, "Order qty"))),
    h("tbody", {}, inv.map((r) => h("tr", {}, h("td", {}, h("b", {}, r.short_name), r.stock_expired ? h("div", { class: "faint", style: { color: "var(--red)" } }, `${r.stock_expired} expired, not counted`) : null),
      h("td", { class: "r" }, `${fmt.int(r.stock_usable)} ${r.unit}`), h("td", {}, runway(r)), h("td", {}, riskChip(r.risk)), h("td", {}, r.reorder_overdue ? h("span", { class: "chip critical" }, "Overdue") : fmt.date(r.reorder_by)), h("td", { class: "r" }, r.reorder_qty ? fmt.int(r.reorder_qty) : "-"))))));
  const actions = c.recommended_actions.length ? h("div", { class: "stack" }, c.recommended_actions.map((a) => h("div", { class: `item ${a.risk}` }, h("h3", {}, riskChip(a.risk), a.medicine), h("div", {}, a.action)))) : empty("Nothing to do", "Stock covers demand past the resupply time.");
  const near = h("div", { class: "tablewrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Nearby clinic"), h("th", { class: "r" }, "Distance"), h("th", { class: "r" }, "Drive"), h("th", { class: "r" }, "Spare units"))),
    h("tbody", {}, c.nearby.slice(0, 5).map((n) => h("tr", { class: "clickable", onClick: () => app.navigate(`clinics/${n.id}`) }, h("td", {}, h("b", {}, n.name), h("div", { class: "faint" }, riskChipText(n.worst_risk))), h("td", { class: "r" }, `${fmt.num(n.distance_km, 0)} km`), h("td", { class: "r" }, `${fmt.num(n.travel_hours, 1)} h`), h("td", { class: "r" }, fmt.int(n.surplus_units)))))));
  return h("div", {},
    h("a", { href: "#/clinics" }, "All clinics"),
    pageHeader(cl.name, `${meta.regions.find((r) => r.id === cl.region)?.name}. Health worker: ${cl.health_worker}, ${cl.phone}.`, h("a", { class: "btn", href: "#/inventory?clinic=" + cl.id }, "Edit stock")),
    ribbon,
    h("div", { class: "grid cols-main" }, panel("Medicine stock", h("div", { class: "stack" }, invTable, runwayKey()), { hint: "Stock is counted only if unexpired. Cover is projected from the disease forecast, not just past use." }),
      h("div", { class: "stack" }, panel("What to do", actions), panel("Nearby clinics", near, { hint: "Closest first; transfers need approval on the Redistribution page." }))),
    h("div", { style: { height: "16px" } }), h("div", { class: "grid cols-2" }, trends));
}
const riskChipText = (r) => `${RISK_LABEL[r]} risk`;
