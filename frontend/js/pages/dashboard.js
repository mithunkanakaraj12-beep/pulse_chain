import { h, fmt, kpi, panel, pageHeader, riskChip, runway, runwayKey, RISK_HEX, RISK_ORDER, empty } from "../lib/ui.js";
import { forecastChart, chartLegend, barChart, stackBar, spark } from "../lib/charts.js";
import { renderMap } from "../lib/map.js";
import * as api from "../lib/api.js";

export async function render({ app, meta }) {
  const d = await api.get("/api/dashboard");
  const k = d.kpis;
  const regionNames = Object.fromEntries(meta.regions.map((r) => [r.id, r.name]));
  const mal = d.disease_activity.find((x) => x.disease === "malaria") || d.disease_activity[0];

  const ribbon = h("div", { class: "ribbon" },
    kpi(k.clinics_at_risk, "Clinics at risk", { unit: `of ${k.clinics_monitored}`, tone: k.clinics_at_risk ? "warn" : "good", detail: "Running short of a medicine before resupply can arrive" }),
    kpi(k.pairs_critical + k.pairs_high, "Medicines needing action", { tone: k.pairs_critical ? "danger" : k.pairs_high ? "warn" : "good", detail: `${k.pairs_critical} critical, ${k.pairs_high} high` }),
    kpi(k.active_alerts, "Open alerts", { tone: k.active_alerts_critical ? "danger" : "", detail: k.active_alerts_critical ? `${k.active_alerts_critical} critical` : "None critical" }),
    kpi(fmt.num(k.network_malaria_next4_avg, 0), "Malaria cases a week, next 4 weeks", { detail: `${fmt.pct(k.network_malaria_vs_seasonal_pct)} vs the usual for this season` }),
    kpi(k.redistribution_opportunities, "Transfers available", { tone: "good", detail: `${fmt.int(k.redistribution_units)} units could move between clinics` }),
    kpi(fmt.int(k.expired_units_on_shelf), "Expired units on shelves", { tone: k.expired_units_on_shelf ? "warn" : "", detail: "Counted as unusable everywhere" }));

  // Malaria outlook (network total)
  const histN = mal.history.map((p) => ({ x: p.week_start, y: p.cases }));
  const outlook = h("div", {},
    forecastChart({ history: histN, forecast: { x: mal.forecast_dates, expected: mal.expected, lower: mal.lower, upper: mal.upper },
      lines: [{ name: "Usual for this season", color: "#0f8b8d", dash: "5 4", y: mal.seasonal_norm }], ariaLabel: "Network malaria cases: last 26 weeks observed and next 8 weeks forecast" }),
    chartLegend([{ label: "Observed", color: "var(--ink)" }, { label: "Forecast", color: "var(--blue)" }, { label: "80% range", color: "var(--blue)", kind: "box" }, { label: "Seasonal norm", color: "#0f8b8d", kind: "dash" }]));

  // Supply thinness: runways of the most urgent clinic x medicine pairs
  const urgent = [...d.replenishments].sort((a, b) => (RISK_ORDER[b.risk] - RISK_ORDER[a.risk]) || ((a.days_to_stockout ?? 999) - (b.days_to_stockout ?? 999))).slice(0, 7);
  const rb = d.risk_distribution.by_clinic_medicine;
  const thin = h("div", { class: "stack" },
    h("div", {}, stackBar([{ label: "Low", value: rb.low, color: RISK_HEX.low }, { label: "Watch", value: rb.moderate, color: RISK_HEX.moderate }, { label: "High", value: rb.high, color: RISK_HEX.high }, { label: "Critical", value: rb.critical, color: RISK_HEX.critical }]),
      chartLegend([{ label: `Low ${rb.low}`, color: RISK_HEX.low }, { label: `Watch ${rb.moderate}`, color: RISK_HEX.moderate }, { label: `High ${rb.high}`, color: RISK_HEX.high }, { label: `Critical ${rb.critical}`, color: RISK_HEX.critical }])),
    urgent.length ? h("div", { class: "tablewrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Clinic and medicine"), h("th", {}, "Stock runway"), h("th", { class: "r" }, "Left"))),
      h("tbody", {}, urgent.map((r) => h("tr", { class: "clickable", onClick: () => app.navigate(`clinics/${r.clinic_id}`) },
        h("td", {}, h("b", {}, r.clinic_name), h("div", { class: "faint" }, r.short_name)), h("td", {}, runway(r)), h("td", { class: "r" }, riskChip(r.risk), h("div", { class: "faint" }, fmt.days(r.days_to_stockout)))))))) : empty("No shortages ahead", "Every clinic has enough stock to last past its resupply time."),
    runwayKey());

  // Disease cards
  const diseases = h("div", { class: "grid cols-3" }, d.disease_activity.map((x) => {
    const rel = x.vs_seasonal_pct;
    return h("div", { class: "panel" }, h("div", { class: "pb" }, h("div", { class: "row between" }, h("h3", {}, x.name), spark(x.history.slice(-16).map((p) => p.cases).concat(x.expected.slice(0, 4)))),
      h("div", { style: { fontSize: "1.6rem", fontWeight: 700 } }, fmt.num(x.next4_avg, 0), h("small", { class: "muted", style: { fontSize: ".85rem", fontWeight: 500 } }, " cases a week, next 4 weeks")),
      h("div", { class: "muted", style: { fontSize: ".85rem" } }, `Last 4 weeks: ${fmt.num(x.recent4_avg, 0)}. `, h("span", { class: `chip ${rel > 40 ? "high" : rel > 15 ? "moderate" : "low"}` }, `${fmt.pct(rel)} vs seasonal`))));
  }));

  // Network cover by medicine (weeks of stock at forecast demand)
  const cover = d.inventory_levels.map((m) => ({ label: m.short_name, weeks: m.demand_next_4_weeks ? (m.usable_stock / (m.demand_next_4_weeks / 4)) : 0, at: m.clinics_at_risk }));
  const coverChart = barChart({ data: cover.map((c) => ({ label: c.label, values: [{ v: Number(c.weeks.toFixed(1)), color: c.at ? "var(--orange)" : "var(--teal)", name: "Weeks of stock:" }] })), unit: "weeks", yMax: Math.max(12, Math.ceil(Math.max(...cover.map((c) => c.weeks)))), ariaLabel: "Weeks of usable stock across the network, by medicine", format: (v) => fmt.num(v, v < 10 ? 1 : 0) });

  const map = renderMap({ points: d.clinic_points, regionNames, onSelect: (id) => app.navigate(`clinics/${id}`) });

  const alerts = d.top_alerts.length ? h("div", { class: "stack" }, d.top_alerts.slice(0, 4).map((a) => h("div", { class: `item ${a.severity}` },
    h("h3", {}, riskChip(a.severity), a.title), h("div", { class: "muted" }, `${a.clinic_name}: ${a.message}`), h("div", {}, h("b", {}, a.recommendation)))),
    h("a", { class: "btn small", href: "#/alerts" }, "Open Alerts Center")) : empty("No open alerts", "Nothing needs attention right now.");

  return h("div", {},
    pageHeader("Network overview", `Forecasts and stock positions as of ${fmt.dateY(d.as_of)}. Demand is forecast 8 weeks ahead from rainfall, temperature, seasonality and recent cases.`,
      h("a", { class: "btn primary", href: "#/demo" }, "Run the monsoon demo")),
    ribbon,
    h("div", { class: "grid cols-main" },
      h("div", { class: "stack" }, panel("Malaria outlook, whole network", outlook, { hint: "Weekly cases across all clinics. The shaded band is an 80% prediction range." }), diseases),
      panel("Where supply is thin", thin, { hint: "Stock cover in days, against how long resupply takes." })),
    h("div", { style: { height: "16px" } }),
    h("div", { class: "grid cols-main" },
      panel("Clinic map", map, { hint: "Select a clinic to open its page." }),
      h("div", { class: "stack" }, panel("Network stock cover", coverChart, { hint: "Weeks of usable stock at forecast demand. Orange: some clinics are short." }), panel("Needs attention", alerts))));
}
