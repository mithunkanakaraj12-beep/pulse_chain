import { h, fmt, kpi, panel, pageHeader, seg, empty, errorBox } from "../lib/ui.js";
import { forecastChart, chartLegend, barChart } from "../lib/charts.js";
import * as api from "../lib/api.js";
import { signalLine, DISEASE_COLOR } from "./shared.js";

export async function render({ meta, app, query }) {
  const st = { disease: query.get("disease") || "malaria", region: "all", clinic: "", weeks: 8 };
  const out = h("div", {});
  const clinics = (await api.get("/api/clinics")).clinics;
  const controls = h("div", { class: "row", style: { alignItems: "flex-end" } });
  const results = h("div", { class: "stack" });

  async function draw() {
    results.replaceChildren(h("div", { class: "skeleton" }));
    try {
      const q = new URLSearchParams({ disease: st.disease, region: st.region, weeks: st.weeks });
      if (st.clinic) q.set("clinic_id", st.clinic);
      const { data: d, cached } = await api.getWithMeta(`/api/forecast?${q}`);
      results.replaceChildren(...view(d, cached).filter(Boolean));
    } catch (e) { results.replaceChildren(errorBox(e, draw)); }
  }
  function drawControls() {
    controls.replaceChildren(
      h("div", { class: "field" }, h("span", {}, "Disease"), seg(meta.diseases.map((d) => [d.id, d.name]), st.disease, (v) => { st.disease = v; draw(); }, "Disease")),
      h("div", { class: "field" }, h("label", { for: "fr" }, "Area"), h("select", { id: "fr", onChange: (e) => { st.region = e.target.value; st.clinic = ""; drawControls(); draw(); } },
        h("option", { value: "all", selected: st.region === "all" }, "Whole network"), meta.regions.map((r) => h("option", { value: r.id, selected: st.region === r.id }, r.name)))),
      h("div", { class: "field" }, h("label", { for: "fc" }, "Single clinic"), h("select", { id: "fc", onChange: (e) => { st.clinic = e.target.value; draw(); } },
        h("option", { value: "" }, "All clinics in the area"), clinics.filter((c) => st.region === "all" || c.region === st.region).map((c) => h("option", { value: c.id, selected: st.clinic === c.id }, c.name)))),
      h("div", { class: "field", style: { minWidth: "190px" } }, h("label", { for: "fw" }, `Look ahead: ${st.weeks} weeks`), h("input", { id: "fw", type: "range", min: 4, max: 8, value: st.weeks, onChange: (e) => { st.weeks = +e.target.value; drawControls(); draw(); } })));
  }

  function view(d, cached) {
    const s = d.summary, f = d.forecast, color = DISEASE_COLOR[d.disease] || "var(--blue)";
    const sc = d.scenario && d.scenario.monsoon ? h("div", { class: "callout warn" }, h("b", {}, "Monsoon scenario applied. "), "This forecast uses simulated extra rainfall. Return to normal from the banner above to see the baseline.") : null;
    const ribbon = h("div", { class: "ribbon" },
      kpi(fmt.num(s.forecast_avg_weekly_cases, 0), "Expected cases a week", { detail: `Last 4 weeks: ${fmt.num(s.recent_4wk_avg_cases, 0)}`, tone: s.change_vs_seasonal_norm_pct > 40 ? "warn" : "" }),
      kpi(fmt.pct(s.change_vs_seasonal_norm_pct), "vs the usual for this season", { detail: `Usual: ${fmt.num(s.seasonal_norm_avg_weekly_cases, 0)} cases a week` }),
      kpi(fmt.num(s.peak_expected_cases, 0), "Peak week", { detail: fmt.date(s.peak_week) }),
      kpi(`${f.expected.length}`, "Weeks forecast", { unit: "weeks", detail: `${d.clinic_count} clinic${d.clinic_count > 1 ? "s" : ""} included` }));
    const chart = h("div", {}, forecastChart({ height: 300, history: d.history.map((p) => ({ x: p.week_start, y: p.cases })), forecast: { x: d.forecast_dates, expected: f.expected, lower: f.lower, upper: f.upper }, color,
      lines: [{ name: "Seasonal norm", color: "#0f8b8d", dash: "5 4", y: f.baseline_seasonal }, { name: "Last-4-weeks average", color: "#8a97ab", dash: "2 3", y: f.baseline_persistence }], ariaLabel: `${d.disease_name} forecast` }),
      chartLegend([{ label: "Observed", color: "var(--ink)" }, { label: "AI forecast", color }, { label: "80% prediction range", color, kind: "box" }, { label: "Simple baseline: seasonal norm", color: "#0f8b8d", kind: "dash" }, { label: "Simple baseline: recent average", color: "#8a97ab", kind: "dash" }]),
      h("p", { class: "hint", style: { marginTop: "8px" } }, d.uncertainty_note));
    // drivers
    const dv = d.drivers, parts = Object.entries(dv.avg_weekly_cases_vs_typical).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
    const maxAbs = Math.max(...parts.map((p) => Math.abs(p[1])), 0.5);
    const drivers = h("div", { class: "stack" }, h("p", { class: "muted", style: { margin: 0 } }, signalLine({ expected_avg: s.forecast_avg_weekly_cases, seasonal_avg: s.seasonal_norm_avg_weekly_cases, ratio_vs_seasonal: s.forecast_avg_weekly_cases / (s.seasonal_norm_avg_weekly_cases || 1), peak_week: s.peak_week })),
      h("div", {}, parts.map(([name, v]) => h("div", { class: "rainbar", style: { gridTemplateColumns: "190px 1fr 70px" } }, h("span", {}, name),
        h("div", { class: "bar", style: { background: "var(--line-2)" } }, h("b", { style: { left: v >= 0 ? "50%" : `${50 - (Math.abs(v) / maxAbs) * 50}%`, width: `${(Math.abs(v) / maxAbs) * 50}%`, background: v >= 0 ? "var(--orange)" : "var(--teal)" } }), h("span", { style: { position: "absolute", left: "50%", top: 0, bottom: 0, width: "1px", background: "var(--ink-3)" } })),
        h("b", { style: { textAlign: "right", color: v >= 0 ? "var(--orange)" : "var(--teal)" } }, `${v > 0 ? "+" : ""}${fmt.num(v, 1)}`)))),
      h("p", { class: "hint" }, "Cases a week pushed up (orange) or down (teal) by each factor compared with typical conditions for this time of year. ", dv.method),
      h("h3", {}, "What the model relies on most overall"),
      h("div", {}, Object.entries(dv.global_importance_pct).sort((a, b) => b[1] - a[1]).map(([n, v]) => h("div", { class: "rainbar", style: { gridTemplateColumns: "190px 1fr 52px" } }, h("span", {}, n), h("div", { class: "bar" }, h("b", { style: { width: `${v}%` } })), h("b", { style: { textAlign: "right" } }, `${fmt.num(v, 0)}%`)))),
      h("p", { class: "hint" }, "Share of the model's accuracy that depends on each factor group, measured on held-out weeks (grouped permutation importance). This is correlation in simulated data, not proof of cause."));
    // demand
    const demand = d.demand.length ? h("div", { class: "tablewrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Medicine"), h("th", { class: "r" }, `Needed over ${f.expected.length} weeks`), h("th", { class: "r" }, "Usual for season"), h("th", { class: "r" }, "Difference"))),
      h("tbody", {}, d.demand.map((m) => h("tr", {}, h("td", {}, h("b", {}, m.short_name), h("div", { class: "faint" }, `${m.units_per_case} ${m.unit} per case`)), h("td", { class: "r" }, `${fmt.int(m.total_expected)} ${m.unit}`), h("td", { class: "r" }, fmt.int(m.total_seasonal_norm)), h("td", { class: "r" }, h("span", { class: `chip ${m.uplift_vs_norm_pct > 40 ? "high" : m.uplift_vs_norm_pct > 15 ? "moderate" : "low"}` }, fmt.pct(m.uplift_vs_norm_pct)))))))) : empty("No medicines linked", "");
    const risk = d.clinics_at_risk.length ? h("div", { class: "tablewrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Clinic"), h("th", {}, "Medicine"), h("th", { class: "r" }, "Stock lasts"))),
      h("tbody", {}, d.clinics_at_risk.slice(0, 8).map((r) => h("tr", { class: "clickable", onClick: () => app.navigate(`clinics/${r.clinic_id}`) }, h("td", {}, r.clinic_name), h("td", {}, r.short_name || r.medicine_name), h("td", { class: "r" }, fmt.days(r.days_to_stockout))))))) : h("p", { class: "muted" }, "No clinics are projected to run short of these medicines within their resupply time.");
    const ev = d.model_evaluation;
    const evPanel = ev ? h("div", { class: "stack" }, barChart({ height: 170, data: [{ label: "AI model", values: [{ v: ev.model_mae, color: "var(--blue)", name: "Error:" }] }, { label: "Seasonal norm", values: [{ v: ev.baseline_mae.seasonal_climatology, color: "#8a97ab", name: "Error:" }] }, { label: "Recent average", values: [{ v: ev.baseline_mae.persistence_4wk_mean, color: "#b9c4d3", name: "Error:" }] }], unit: "cases", format: (v) => fmt.num(v, 2), ariaLabel: "Average error per clinic-week on held-out weeks" }),
      h("p", { class: "hint" }, `Average miss per clinic-week on ${fmt.date(ev.test_window[0])} to ${fmt.date(ev.test_window[1])}, weeks the model never trained on. Lower is better. ${ev.mae_improvement_vs_best_baseline_pct >= 0 ? `The model is ${fmt.num(ev.mae_improvement_vs_best_baseline_pct, 1)}% better than the best simple baseline.` : "The model is not better than the best simple baseline here."} The 80% range covered ${fmt.num(ev.interval.empirical * 100, 0)}% of held-out weeks.`),
      h("a", { href: "#/model" }, "Full evaluation and limits")) : null;
    return [sc, cached ? h("div", { class: "callout" }, "Showing the last forecast saved on this device. Forecasts cannot be recalculated while offline.") : null, ribbon,
      h("div", { class: "grid cols-main" },
        h("div", { class: "stack" }, panel(`${d.disease_name} forecast, ${d.clinic_id ? clinics.find((c) => c.id === d.clinic_id)?.name : d.region === "all" ? "whole network" : meta.regions.find((r) => r.id === d.region)?.name}`, chart, { hint: "Weekly cases. History is simulated; the forecast is the model's output." }),
          panel("Medicine needed because of these cases", demand, { hint: "Cases times the treatment protocol for each medicine." }), panel("Clinics that may run short", risk)),
        h("div", { class: "stack" }, panel("Why this forecast", drivers), panel("How good is this model?", evPanel || "")))];
  }

  drawControls(); await draw();
  out.append(pageHeader("Forecasting Center", "Eight-week disease forecasts from a machine-learning model, with an honest uncertainty range, simple baselines for comparison, and the factors behind each forecast."),
    h("div", { class: "callout sim", style: { marginBottom: "14px" } }, "These forecasts are trained on simulated data. They show how the system behaves; they are not epidemiological predictions for any real place."),
    panel(null, controls), h("div", { style: { height: "16px" } }), results);
  return out;
}
