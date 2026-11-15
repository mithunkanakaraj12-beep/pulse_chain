import { h, fmt, panel, pageHeader, riskChip, runway, runwayKey, RISK_ORDER, seg, toast, empty, debounce, kpi } from "../lib/ui.js";
import * as api from "../lib/api.js";

export async function render({ meta, app, query }) {
  const st = { clinic: query.get("clinic") || "", risk: "all", sort: "risk", q: "", open: null };
  const clinics = (await api.get("/api/clinics")).clinics;
  let rows = [];
  const holder = h("div", {});
  const ribbonBox = h("div", {});

  async function load() {
    const url = st.clinic ? `/api/inventory?clinic_id=${st.clinic}` : "/api/inventory";
    const { data, cached } = await api.getWithMeta(url);
    rows = data.rows; draw(cached);
  }
  function draw(cached) {
    const c = { critical: 0, high: 0, expired: 0 };
    rows.forEach((r) => { if (r.risk === "critical") c.critical++; if (r.risk === "high") c.high++; c.expired += r.stock_expired; });
    ribbonBox.replaceChildren(h("div", { class: "ribbon" }, kpi(rows.length, "Clinic and medicine pairs"), kpi(c.critical, "Critical", { tone: c.critical ? "danger" : "good" }), kpi(c.high, "High risk", { tone: c.high ? "warn" : "good" }), kpi(fmt.int(c.expired), "Expired units", { tone: c.expired ? "warn" : "", detail: "Not counted as stock" })));
    let list = rows.filter((r) => (st.risk === "all" || r.risk === st.risk) && `${r.clinic_name} ${r.short_name}`.toLowerCase().includes(st.q.toLowerCase()));
    list.sort((a, b) => st.sort === "risk" ? (RISK_ORDER[b.risk] - RISK_ORDER[a.risk]) || ((a.days_to_stockout ?? 999) - (b.days_to_stockout ?? 999)) : st.sort === "clinic" ? a.clinic_name.localeCompare(b.clinic_name) : (a.days_to_stockout ?? 999) - (b.days_to_stockout ?? 999));
    holder.replaceChildren(...[
      cached ? h("div", { class: "callout", style: { marginBottom: "10px" } }, "Offline: showing saved stock. Edits you make are kept on this device and sync when you reconnect; projections update after sync.") : null,
      list.length ? h("div", { class: "tablewrap" }, h("table", { class: "t" }, h("thead", {}, h("tr", {}, h("th", {}, "Clinic"), h("th", {}, "Medicine"), h("th", { class: "r" }, "Usable"), h("th", { class: "r" }, "Daily use"), h("th", {}, "Stock runway"), h("th", {}, "Risk"), h("th", {}, "Reorder"), h("th", {}))),
        h("tbody", {}, list.flatMap((r) => rowEls(r))))) : empty("Nothing matches", "Change the risk filter or search.")].filter(Boolean));
  }
  const key = (r) => `${r.clinic_id}:${r.medicine_id}`;
  function rowEls(r) {
    const open = st.open === key(r);
    const main = h("tr", {}, h("td", {}, h("a", { href: `#/clinics/${r.clinic_id}` }, r.clinic_name)), h("td", {}, h("b", {}, r.short_name)),
      h("td", { class: "r" }, `${fmt.int(r.stock_usable)} ${r.unit}`, r.stock_expired ? h("div", { style: { color: "var(--red)", fontSize: ".78rem" } }, `+${r.stock_expired} expired`) : null),
      h("td", { class: "r" }, fmt.num(r.forecast_daily_next4w ?? r.avg_daily_usage, 1)), h("td", {}, runway(r), h("div", { class: "faint", style: { fontSize: ".78rem" } }, fmt.days(r.days_to_stockout))),
      h("td", {}, riskChip(r.risk)), h("td", {}, r.reorder_qty ? h("div", {}, h("b", {}, fmt.int(r.reorder_qty)), h("div", { class: "faint", style: { fontSize: ".78rem" } }, r.reorder_overdue ? "overdue" : `by ${fmt.date(r.reorder_by)}`)) : "-"),
      h("td", {}, h("button", { class: "btn small", "aria-expanded": String(open), onClick: () => { st.open = open ? null : key(r); draw(); } }, open ? "Close" : "Edit")));
    if (!open) return [main];
    return [main, h("tr", {}, h("td", { colspan: 8, style: { background: "var(--canvas)" } }, editor(r)))];
  }

  function editor(r) {
    const save = async (label, fn) => { try { const res = await fn(); if (res.queued) toast(`${label} saved on this device. It will sync when you are back online.`, "warn"); else { toast(`${label} saved. Projections updated.`); } await load(); } catch (e) { toast(e.message, "error"); } };
    const batches = r.batches.map((b) => {
      const q = h("input", { type: "number", min: 0, value: b.quantity, "aria-label": `Quantity, batch ${b.id}` }), e = h("input", { type: "date", value: b.expiry_date, "aria-label": `Expiry, batch ${b.id}` });
      return h("div", { class: `batch ${b.expired ? "expired" : ""}` }, h("div", {}, h("b", {}, `Batch ${b.id}`), h("div", { class: "faint", style: { fontSize: ".78rem" } }, `${b.origin === "transfer" ? "Received by transfer" : "In stock"}, received ${fmt.date(b.received_date)}${b.expired ? ", expired" : ""}`)), h("div", { class: "row" }, q, e),
        h("button", { class: "btn small primary", onClick: () => save("Batch", () => api.patch(`/api/inventory/batches/${b.id}`, { quantity: Number(q.value), expiry_date: e.value }, { label: `Batch ${b.id} edit` })) }, "Save"));
    });
    const nq = h("input", { type: "number", min: 1, value: 100, "aria-label": "New batch quantity" }), ne = h("input", { type: "date", "aria-label": "New batch expiry", value: new Date(Date.parse(meta.demo_today) + 365 * 864e5).toISOString().slice(0, 10) });
    const pol = h("input", { type: "number", min: 0, value: r.min_safety_stock, style: { width: "100px" }, "aria-label": "Minimum safety stock" });
    return h("div", { class: "grid cols-2", style: { padding: "6px 4px" } },
      h("div", {}, h("h3", {}, "Batches"), batches.length ? batches : h("p", { class: "muted" }, "No batches. Receive stock below."),
        h("div", { class: "row", style: { marginTop: "10px" } }, h("b", {}, "Receive new stock"), nq, ne, h("button", { class: "btn small teal", onClick: () => save("New batch", () => api.post("/api/inventory/batches", { clinic_id: r.clinic_id, medicine_id: r.medicine_id, quantity: Number(nq.value), expiry_date: ne.value }, { label: "New batch" })) }, "Add batch"))),
      h("div", { class: "stack" }, h("div", {}, h("h3", {}, "Why this projection"), h("p", { class: "muted", style: { margin: "4px 0" } }, r.risk_reason),
        h("table", { class: "t" }, h("tbody", {}, [["Demand during resupply", `${fmt.int(r.demand_over_lead_time)} ${r.unit}`], ["Safety stock held back", `${fmt.int(r.safety_stock_units)} ${r.unit}`], ["Stockout, high / low demand", `${fmt.days(r.days_to_stockout_pessimistic)} / ${fmt.days(r.days_to_stockout_optimistic)}`], ["Expected to expire unused", `${fmt.int(r.expected_expiry_waste)} ${r.unit}`], ["Spare stock to share", `${fmt.int(r.surplus_units)} ${r.unit}`]].map(([a, b]) => h("tr", {}, h("td", { class: "muted" }, a), h("td", { class: "r" }, b)))))),
        h("div", { class: "row" }, h("label", { class: "field" }, "Minimum safety stock", pol), h("button", { class: "btn small", style: { marginTop: "18px" }, onClick: () => save("Safety stock", () => api.put(`/api/inventory/${r.clinic_id}/${r.medicine_id}/policy`, { min_safety_stock: Number(pol.value) }, { label: "Safety stock change" })) }, "Save policy"))));
  }

  await load();
  const controls = h("div", { class: "row", style: { alignItems: "flex-end" } },
    h("div", { class: "field" }, h("label", { for: "ic" }, "Clinic"), h("select", { id: "ic", onChange: async (e) => { st.clinic = e.target.value; st.open = null; await load(); } }, h("option", { value: "" }, "All clinics"), clinics.map((c) => h("option", { value: c.id, selected: st.clinic === c.id }, c.name)))),
    h("div", { class: "field" }, h("span", {}, "Risk"), seg([["all", "All"], ["critical", "Critical"], ["high", "High"], ["moderate", "Watch"], ["low", "Low"]], "all", (v) => { st.risk = v; draw(); }, "Risk filter")),
    h("div", { class: "field" }, h("label", { for: "iq" }, "Search"), h("input", { id: "iq", type: "search", placeholder: "Clinic or medicine", onInput: debounce((e) => { st.q = e.target.value; draw(); }, 120) })));
  return h("div", {}, pageHeader("Inventory", "Edit batches and safety stock; every projection, alert and transfer proposal recalculates from the new numbers. Stock past its expiry date is never counted."),
    ribbonBox, panel(null, h("div", { class: "stack" }, controls, runwayKey(), holder)));
}
