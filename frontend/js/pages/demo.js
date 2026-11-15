// The 8-step monsoon walkthrough. Everything shown is computed by the backend from a SIMULATED rainfall overlay
// (read-only endpoint); nothing here is a hard-coded result and nothing is a scientific claim.
import { h, fmt, panel, pageHeader, riskChip, runway, runwayKey, toast, kpi, RISK_ORDER, RISK_HEX, seg, errorBox, confirmDialog } from "../lib/ui.js";
import { compareChart, forecastChart, chartLegend, stackBar, barChart } from "../lib/charts.js";
import { renderMap } from "../lib/map.js";
import * as api from "../lib/api.js";

const STEPS = [
  ["normal", "Normal conditions"], ["monsoon", "Monsoon arrives"], ["forecast", "AI forecast"], ["inventory_impact", "Medicine demand"],
  ["shortages", "Shortages detected"], ["sms", "Alerts by SMS"], ["redistribution", "Redistribution"], ["final", "Result"],
];

export async function render({ meta, app }) {
  const sc = meta.scenario || {};
  const cfg = { intensity: 1.0, weeks: 3, regions: meta.regions.map((r) => r.id) };
  const regionNames = Object.fromEntries(meta.regions.map((r) => [r.id, r.name]));
  const st = { i: 0, seen: new Set([0]), timer: null, data: null };
  const root = h("div", {}), stage = h("div", { class: "stage" }), nav = h("ol", { class: "steps", "aria-label": "Demo steps" });

  async function load() {
    stage.replaceChildren(h("div", { class: "skeleton", style: { minHeight: "360px" } }));
    try {
      const q = new URLSearchParams({ intensity: cfg.intensity, weeks: cfg.weeks, regions: cfg.regions.join(",") });
      st.data = await api.get(`/api/scenario/walkthrough?${q}`);
      show(st.i);
    } catch (e) { stage.replaceChildren(errorBox(e, load)); }
  }
  function go(i) { stopAuto(); show(Math.max(0, Math.min(STEPS.length - 1, i))); }
  function show(i) {
    st.i = i; st.seen.add(i);
    nav.replaceChildren(...STEPS.map(([k, t], j) => h("li", {}, h("button", { "aria-current": j === i ? "step" : null, class: st.seen.has(j) && j !== i ? "seen" : "", onClick: () => go(j) }, t))));
    const [key, title] = STEPS[i], body = STEPS_VIEW[key](st.data.steps[key], st.data);
    stage.replaceChildren(h("div", { class: "panel" }, h("div", { class: "pb" },
      h("div", { class: "stage-head" }, h("div", {}, h("span", { class: "tag" }, `Step ${i + 1} of ${STEPS.length}`), h("h2", { style: { fontSize: "1.45rem", marginTop: "6px" } }, title))),
      body,
      h("div", { class: "stage-actions" }, h("button", { class: "btn", disabled: i === 0, onClick: () => go(i - 1) }, "Back"),
        i < STEPS.length - 1 ? h("button", { class: "btn primary", onClick: () => go(i + 1) }, `Next: ${STEPS[i + 1][1]}`) : h("button", { class: "btn", onClick: () => go(0) }, "Start again"),
        h("button", { class: "btn ghost", onClick: st.timer ? stopAuto : autoplay }, st.timer ? "Pause" : "Play through")))));
  }
  function autoplay() { if (st.i >= STEPS.length - 1) st.i = -1; st.timer = setInterval(() => { if (st.i >= STEPS.length - 1) return stopAuto(); show(st.i + 1); }, 5200); show(Math.max(st.i, 0) === st.i && st.i >= 0 ? st.i : 0); }
  function stopAuto() { if (st.timer) { clearInterval(st.timer); st.timer = null; if (st.data) show(st.i); } }

  const note = (t) => h("p", { class: "hint", style: { marginTop: "8px" } }, t);
  const SIMTAG = h("div", { class: "callout sim", style: { marginBottom: "12px" } }, st.data ? "" : "");

  const STEPS_VIEW = {
    normal: (s) => h("div", { class: "stack" }, h("p", { class: "muted" }, "A typical late-May week. Rainfall is ordinary, malaria is at its seasonal level and most clinics have enough stock to reach the next delivery."),
      h("div", { class: "grid cols-2" }, h("div", {}, renderMap({ points: s.clinic_points, regionNames })),
        h("div", { class: "stack" }, h("div", { class: "stat-pair" }, h("div", {}, h("div", { class: "a" }, fmt.num(s.malaria_next4_avg, 0)), h("div", { class: "muted" }, "expected malaria cases a week, whole network")), h("div", {}, h("div", { class: "a" }, s.transfers_available), h("div", { class: "muted" }, "routine transfers available"))),
          stackBar([["low", "Low"], ["moderate", "Watch"], ["high", "High"], ["critical", "Critical"]].map(([k, l]) => ({ label: l, value: s.risk_distribution[k], color: RISK_HEX[k] }))),
          chartLegend([["low", "Low"], ["moderate", "Watch"], ["high", "High"], ["critical", "Critical"]].map(([k, l]) => ({ label: `${l} ${s.risk_distribution[k]}`, color: RISK_HEX[k] }))),
          h("p", { class: "hint" }, "Each count is a clinic and medicine pair, rated by how long stock lasts compared with how long resupply takes.")))),
    monsoon: (s) => h("div", { class: "stack" }, h("p", { class: "muted" }, `Suppose heavy monsoon rain falls for ${s.weeks} week${s.weeks > 1 ? "s" : ""} in the selected regions. In this demo that is a simulated change to the rainfall data, ${s.intensity}x the normal level plus a flood-season boost. It is applied in memory only; the stored data is untouched.`),
      h("div", {}, s.rain.map((r) => { const max = Math.max(...s.rain.map((x) => Math.max(x.normal_weekly_rain_mm, x.simulated_weekly_rain_mm)), 1);
        return h("div", { style: { margin: "10px 0" } }, h("div", { class: "row between" }, h("b", {}, r.name), r.affected ? h("span", { class: "chip high" }, "Monsoon applied") : h("span", { class: "chip neutral" }, "Unchanged")),
          h("div", { class: "rainbar" }, h("span", { class: "muted" }, "Normal"), h("div", { class: "bar" }, h("b", { style: { width: `${(r.normal_weekly_rain_mm / max) * 100}%` } })), h("span", {}, `${fmt.num(r.normal_weekly_rain_mm, 0)} mm a week`)),
          h("div", { class: "rainbar" }, h("span", { class: "muted" }, "Simulated"), h("div", { class: "bar" }, h("b", { class: "wet", style: { width: `${(r.simulated_weekly_rain_mm / max) * 100}%` } })), h("span", {}, `${fmt.num(r.simulated_weekly_rain_mm, 0)} mm a week`))); })),
      note("This is a what-if on synthetic weather, chosen to be dramatic enough to show the system reacting. It is not a weather forecast.")),
    forecast: (s, all) => { const dis = Object.entries(s);
      return h("div", { class: "stack" }, h("p", { class: "muted" }, "The model re-reads the rainfall and forecasts again. Waterborne and vector-borne diseases respond to rain with a delay of a few weeks, so the effect builds over the forecast."),
        h("div", { class: "grid cols-2" }, dis.map(([d, f]) => h("div", { class: "panel" }, h("div", { class: "pb" }, h("div", { class: "row between" }, h("h3", {}, fmt.title(d)), h("span", { class: `chip ${f.increase_pct > 50 ? "critical" : f.increase_pct > 15 ? "high" : f.increase_pct > 3 ? "moderate" : "low"}` }, `${fmt.pct(f.increase_pct, 0)} over 8 weeks`)),
          compareChart({ dates: f.dates, a: f.baseline, b: f.monsoon, height: 220, ariaLabel: `${d}: normal versus monsoon forecast` }),
          h("div", { class: "muted", style: { fontSize: ".85rem" } }, `Peak in the week of ${fmt.date(f.peak_week)}: ${fmt.num(f.peak_ratio_vs_baseline, 1)}x the normal-weather forecast for that week. ${fmt.int(f.total_baseline)} cases expected normally, ${fmt.int(f.total_monsoon)} with the monsoon.`))))),
        h("div", { class: "callout warn" }, all.disclaimer));
    },
    inventory_impact: (s) => h("div", { class: "stack" }, h("p", { class: "muted" }, "More cases mean more treatment courses, test kits and fluids. Each medicine's need is the forecast cases multiplied by its treatment protocol."),
      barChart({ height: 240, data: s.map((m) => ({ label: m.short_name, values: [{ v: m.demand_8wk_before, color: "var(--blue)", name: "Normal:" }, { v: m.demand_8wk_after, color: "var(--orange)", name: "Monsoon:" }] })), unit: "units over 8 weeks", ariaLabel: "Eight-week medicine need, normal versus monsoon" }),
      chartLegend([{ label: "Normal weather", color: "var(--blue)" }, { label: "Monsoon", color: "var(--orange)" }]),
      h("div", { class: "tablewrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Medicine"), h("th", { class: "r" }, "Normal"), h("th", { class: "r" }, "Monsoon"), h("th", { class: "r" }, "Change"))),
        h("tbody", {}, [...s].sort((a, b) => b.increase_pct - a.increase_pct).map((m) => h("tr", {}, h("td", {}, h("b", {}, m.short_name)), h("td", { class: "r" }, `${fmt.int(m.demand_8wk_before)} ${m.unit}`), h("td", { class: "r" }, `${fmt.int(m.demand_8wk_after)} ${m.unit}`), h("td", { class: "r" }, h("span", { class: `chip ${m.increase_pct > 40 ? "high" : m.increase_pct > 10 ? "moderate" : "low"}` }, fmt.pct(m.increase_pct, 0))))))))),
    shortages: (s) => { const rows = s.rows.slice(0, 10);
      return h("div", { class: "stack" }, h("p", { class: "muted" }, "Stock that looked fine at normal use now runs out sooner. A clinic is flagged when stock will not last until a resupply could arrive."),
        h("div", { class: "grid cols-2" }, ["counts_before", "counts_after"].map((k, n) => h("div", { class: "panel" }, h("div", { class: "pb" }, h("h3", {}, n ? "With monsoon demand" : "Before the monsoon"), stackBar([["low", "Low"], ["moderate", "Watch"], ["high", "High"], ["critical", "Critical"]].map(([r, l]) => ({ label: l, value: s[k][r], color: RISK_HEX[r] }))),
          chartLegend([["low", "Low"], ["moderate", "Watch"], ["high", "High"], ["critical", "Critical"]].map(([r, l]) => ({ label: `${l} ${s[k][r]}`, color: RISK_HEX[r] }))))))),
        rows.length ? h("div", { class: "tablewrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Clinic and medicine"), h("th", {}, "Stock runway with monsoon"), h("th", { class: "r" }, "Stock lasts"), h("th", {}, "Rating"))),
          h("tbody", {}, rows.map((r) => h("tr", {}, h("td", {}, h("b", {}, r.clinic_name), h("div", { class: "faint" }, r.short_name)),
            h("td", {}, runway({ risk: r.risk_after, lead_time_days: r.lead_time_days, days_to_stockout: r.days_after, days_to_stockout_pessimistic: r.days_after_pessimistic, days_to_stockout_optimistic: r.days_after_optimistic })),
            h("td", { class: "r" }, h("b", {}, fmt.days(r.days_after)), h("div", { class: "faint" }, `was ${fmt.days(r.days_before)}`), r.naive_days_cover != null ? h("div", { class: "faint" }, `a simple stock/avg-use count says ${fmt.days(r.naive_days_cover)}`) : null),
            h("td", {}, riskChip(r.risk_before), " to ", riskChip(r.risk_after)))))) : h("p", { class: "muted" }, "No clinic and medicine pair changed rating."), runwayKey());
    },
    sms: (s) => h("div", { class: "stack" }, h("p", { class: "muted" }, "Health workers are notified by plain text message, which works on basic phones with weak signal. Each message names the clinic, the medicine, how many days remain, and what to do."),
      s.messages.length ? h("div", { class: "grid cols-2" }, s.messages.map((m) => h("div", { class: `item ${m.severity}` }, h("div", { class: "row between" }, h("b", {}, m.health_worker), riskChip(m.severity)), h("div", { class: "faint" }, `${m.clinic_name}, ${m.phone}`), h("div", { class: "sms-bubble" }, m.body), h("div", { class: "sms-meta" }, `${m.chars} of 160 characters`)))) : h("p", { class: "muted" }, "No clinic crossed the alert level, so no messages would be sent."),
      h("div", { class: "callout sim" }, "Simulated preview. Nothing was sent. To log a simulated send, or enable real sending deliberately, use the Alerts Center."), note("The phone numbers are fictional.")),
    redistribution: (s) => h("div", { class: "stack" }, h("p", { class: "muted" }, "Before waiting for new stock, the system checks which nearby clinics hold spare, unexpired stock they will not need. It proposes transfers that arrive in time and leave the donor safe."),
      h("div", { class: "grid cols-main" }, renderMap({ points: s.clinic_points, transfers: s.transfers, regionNames, activeTransferId: s.transfers[0]?.id }),
        h("div", { class: "stack" }, s.transfers.length ? s.transfers.slice(0, 5).map((t) => h("div", { class: `item ${t.receiver_risk_before}` }, h("h3", {}, `${fmt.int(t.quantity)} ${t.unit}`, t.medicine_name), h("div", {}, `${t.donor_name} to ${t.receiver_name}`), h("div", { class: "faint" }, `${fmt.num(t.distance_km, 0)} km, arrives ${fmt.date(t.arrival_date)}. Stock lasts ${fmt.days(t.receiver_days_to_stockout_before)} now, ${fmt.days(t.receiver_days_to_stockout_after)} after.`))) : [h("p", { class: "muted" }, "No safe transfers exist for this scenario.")])),
      h("div", { class: "callout" }, "These are proposals. A person approves each one on the Redistribution page; approving in this demo only changes simulated stock.")),
    final: (s, all) => h("div", { class: "stack" }, h("p", { class: "muted" }, "Putting it together: how many clinic and medicine pairs are at risk, and how much the proposed transfers recover."),
      h("div", { class: "ribbon" }, kpi(s.at_risk_before_monsoon, "At risk before", { tone: "good" }), kpi(s.at_risk_with_monsoon, "At risk with monsoon", { tone: "danger" }), kpi(s.at_risk_after_approved_transfers, "At risk after transfers", { tone: "warn", detail: `${s.pairs_improved_by_transfers} pairs improved` }), kpi(fmt.int(s.units_redistributed), "Units moved between clinics"), kpi(fmt.int(s.total_reorder_units_recommended), "Units to reorder", { detail: "Transfers bridge the gap; reordering still needed" })),
      s.lifted.length ? h("div", { class: "tablewrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Clinic and medicine"), h("th", { class: "r" }, "Received"), h("th", {}, "Rating change"), h("th", { class: "r" }, "Stock now lasts"))),
        h("tbody", {}, s.lifted.map((l) => h("tr", {}, h("td", {}, h("b", {}, l.clinic_name), h("div", { class: "faint" }, l.short_name)), h("td", { class: "r" }, fmt.int(l.received_units)), h("td", {}, riskChip(l.risk_before), " to ", riskChip(l.risk_after)), h("td", { class: "r" }, fmt.days(l.days_after))))))) : null,
      h("div", { class: "callout warn" }, all.disclaimer),
      h("div", { class: "row" }, h("button", { class: "btn primary", onClick: applyLive }, "Apply this monsoon to the live dashboard"), h("a", { class: "btn", href: "#/dashboard" }, "Back to dashboard"), h("span", { class: "muted" }, "Applying changes every page until you return to normal."))),
  };

  async function applyLive() {
    const ok = await confirmDialog({ title: "Apply the simulated monsoon everywhere?", body: "Dashboard, forecasts, inventory risk, alerts and transfers will all reflect the simulated rainfall until you choose Return to normal in the banner. Stored data is not changed.", confirmLabel: "Apply monsoon" });
    if (!ok) return;
    try { await api.post("/api/scenario", { monsoon: true, intensity: cfg.intensity, weeks: cfg.weeks, regions: cfg.regions }); await app.refreshMeta(); toast("Monsoon applied. Explore the dashboard, then return to normal from the banner."); location.hash = "#/dashboard"; } catch (e) { toast(e.message, "error"); }
  }

  const controls = h("div", { class: "row", style: { alignItems: "flex-end" } },
    h("div", { class: "field", style: { minWidth: "200px" } }, h("label", { for: "int" }, "Rain intensity"), h("input", { id: "int", type: "range", min: 0.5, max: 2, step: 0.25, value: cfg.intensity, onInput: (e) => { cfg.intensity = +e.target.value; e.target.nextSibling.textContent = `${cfg.intensity}x`; }, onChange: () => { stopAuto(); load(); } }), h("span", { class: "muted" }, "1x")),
    h("div", { class: "field", style: { minWidth: "160px" } }, h("label", { for: "wk" }, "Duration"), h("select", { id: "wk", onChange: (e) => { cfg.weeks = +e.target.value; stopAuto(); load(); } }, [1, 2, 3, 4].map((w) => h("option", { value: w, selected: w === cfg.weeks }, `${w} week${w > 1 ? "s" : ""}`)))),
    h("div", { class: "field" }, h("span", {}, "Regions hit"), h("div", { class: "row" }, meta.regions.map((r) => h("label", { class: "chk" }, h("input", { type: "checkbox", checked: true, onChange: (e) => { cfg.regions = e.target.checked ? [...cfg.regions, r.id] : cfg.regions.filter((x) => x !== r.id); if (!cfg.regions.length) { cfg.regions = [r.id]; e.target.checked = true; } stopAuto(); load(); } }), r.name.replace(" District", ""))))));

  root.append(pageHeader("Monsoon demo", "A guided, eight-step what-if: heavy rain arrives, the model re-forecasts, stock runs short, and the system proposes a response. Use the steps on the left or play it through."),
    h("div", { class: "callout sim", style: { marginBottom: "14px" } }, h("b", {}, "Simulated scenario. "), "Rainfall, cases and stock are all synthetic, and the monsoon is a hypothetical input. The size of the surge is whatever the model produces for that input; it is not a scientific finding about real disease."),
    panel(null, controls), h("div", { style: { height: "16px" } }), h("div", { class: "demo" }, h("div", {}, nav), stage));
  root.addEventListener("keydown", (e) => { if (e.target.matches("input,select,textarea")) return; if (e.key === "ArrowRight") go(st.i + 1); if (e.key === "ArrowLeft") go(st.i - 1); });
  await load();
  return { node: root, cleanup: () => stopAuto() };
}
