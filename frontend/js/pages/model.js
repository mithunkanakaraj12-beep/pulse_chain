import { h, fmt, panel, pageHeader, kpi, seg } from "../lib/ui.js";
import { barChart, chartLegend } from "../lib/charts.js";
import * as api from "../lib/api.js";

export async function render({ meta }) {
  const [m, docs] = await Promise.all([api.get("/api/model/metrics"), api.get("/api/docs")]);
  const diseases = meta.diseases;
  const box = h("div", { class: "stack" });
  let cur = diseases[0].id;
  function draw() {
    const e = m[cur];
    const byH = barChart({ height: 220, yMax: undefined, data: e.by_horizon.map((r) => ({ label: `${r.horizon} wk`, values: [{ v: r.model_mae, color: "var(--blue)", name: "AI model:" }, { v: r.climatology_mae, color: "#0f8b8d", name: "Seasonal norm:" }, { v: r.persistence_mae, color: "#b9c4d3", name: "Recent average:" }] })), unit: "cases", format: (v) => fmt.num(v, 1), ariaLabel: "Error by forecast horizon" });
    const hr = e.heavy_rain_subset;
    const win = e.by_horizon.filter((r) => r.model_mae < r.climatology_mae).length;
    box.replaceChildren(
      h("div", { class: "ribbon" }, kpi(fmt.num(e.model_mae, 2), "Model error", { detail: "Cases per clinic-week, held-out" }), kpi(fmt.num(e.baseline_mae.seasonal_climatology, 2), "Seasonal-norm error"), kpi(fmt.num(e.baseline_mae.persistence_4wk_mean, 2), "Recent-average error"),
        kpi(fmt.pct(e.mae_improvement_vs_best_baseline_pct, 1), "vs best baseline", { tone: e.mae_improvement_vs_best_baseline_pct >= 5 ? "good" : "warn", detail: `Beats seasonal norm at ${win} of 8 horizons` }), kpi(`${fmt.num(e.interval.empirical * 100, 0)}%`, "Range coverage", { detail: `Aimed for ${fmt.num(e.interval.nominal * 100, 0)}%` })),
      h("div", { class: "grid cols-main" }, panel("Error by how far ahead", h("div", {}, byH, chartLegend([{ label: "AI model", color: "var(--blue)" }, { label: "Seasonal norm", color: "#0f8b8d" }, { label: "Recent average", color: "#b9c4d3" }])), { hint: "Average miss per clinic-week for each forecast distance. Lower is better." }),
        panel("Heavy-rain weeks", hr ? h("div", { class: "stack" }, h("p", { style: { margin: 0 } }, `On the ${fmt.int(hr.rows)} held-out clinic-weeks that followed heavy rain, the average miss was ${fmt.num(hr.model_mae, 2)} cases for the model, ${fmt.num(hr.climatology_mae, 2)} for the seasonal norm and ${fmt.num(hr.persistence_mae, 2)} for the recent average.`), h("p", { class: "hint" }, hr.model_mae < hr.climatology_mae ? "The model is ahead of the seasonal norm in these weeks, which is where rainfall information should help most." : "The model is not ahead of the seasonal norm in these weeks.")) : h("p", { class: "muted" }, "No heavy-rain weeks in the test window.")),
    ));
  }
  draw();
  const api_rows = docs.routes.map((r) => h("tr", {}, h("td", {}, r.methods.map((x) => h("span", { class: "tag", style: { marginRight: "4px" } }, x))), h("td", {}, h("code", {}, r.path)), h("td", { class: "muted" }, r.description)));
  return h("div", {}, pageHeader("Model and data", "How the forecasts are made, how well they work on weeks the model never saw, and what they cannot tell you."),
    h("div", { class: "callout sim", style: { marginBottom: "14px" } }, h("b", {}, "All data here is simulated. "), meta.data_label.replace("SIMULATED DATA - ", ""), ". Accuracy numbers describe performance on this synthetic data and say nothing about real-world performance."),
    h("div", { class: "field" }, h("span", {}, "Disease"), seg(diseases.map((d) => [d.id, d.name]), cur, (v) => { cur = v; draw(); }, "Disease")), h("div", { style: { height: "12px" } }), box, h("div", { style: { height: "16px" } }),
    h("div", { class: "grid cols-2" },
      panel("Method", h("div", { class: "stack" }, ...[
        ["Model", meta.model.description],
        ["Targets and horizons", "One model per disease predicts weekly cases per 1,000 people for each of the next 1 to 8 weeks. The horizon is an input feature, so one model serves all eight weeks."],
        ["Inputs", "Recent cases and their trend, rainfall (recent and lagged 2 to 6 weeks), temperature, time of year, catchment size and forecast distance."],
        ["Uncertainty", "The 80% range comes from quantile models, then adjusted on 52 calibration weeks (conformalised quantile regression) so it is not over- or under-confident by construction."],
        ["Validation", "Strictly chronological: train, then 52 weeks to calibrate, then the final 52 weeks held out for the numbers above. No shuffling, so the model never sees the future."],
        ["Baselines", "Seasonal norm (average for that week of the year) and recent average (mean of the last four weeks). A model that cannot beat these is not worth running."],
        ["Explanations", "Per forecast: set each factor to its typical value for the time of year and see how far the forecast moves. Overall: how much held-out accuracy drops when a factor group is shuffled. These show what the model uses, not what causes disease."],
      ].map(([a, b]) => h("div", {}, h("b", {}, a), h("p", { style: { margin: "2px 0 0" }, class: "muted" }, b))))),
      panel("Limits, stated plainly", h("div", { class: "stack" }, ...[
        "The dataset is generated by a program with a built-in rainfall-to-disease relationship. The model recovers that relationship; this does not show it would work on real surveillance data.",
        "Against the seasonal norm the gain is modest, and it shrinks at longer horizons. The model's main contribution is reacting to unusual rain and recent trends.",
        "The 80% range is deliberately conservative: it covered more of the held-out weeks than the 80% aimed for.",
        "Small clinics have few cases per week, so single-clinic forecasts are noisy. Network and regional views are steadier.",
        "Forecasts need the server. Offline, the app shows the last forecasts saved on the device; it cannot compute new ones.",
        "The monsoon scenario is a hypothetical input, not a prediction of weather.",
      ].map((t) => h("p", { style: { margin: 0 } }, t))))),
    h("div", { style: { height: "16px" } }), panel("API reference", h("div", { class: "tablewrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Method"), h("th", {}, "Path"), h("th", {}, "What it does"))), h("tbody", {}, api_rows))), { hint: "Generated from the running server. Also available as JSON at /api/docs." }));
}
