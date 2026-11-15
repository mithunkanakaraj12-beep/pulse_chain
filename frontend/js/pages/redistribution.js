import { h, fmt, panel, pageHeader, riskChip, toast, confirmDialog, empty, kpi } from "../lib/ui.js";
import * as api from "../lib/api.js";
import { renderMap } from "../lib/map.js";

export async function render({ meta, app }) {
  const { data, cached } = await api.getWithMeta("/api/redistribution");
  const regionNames = Object.fromEntries(meta.regions.map((r) => [r.id, r.name]));
  const st = { active: data.proposed[0]?.id || null, clinic: null };
  const mapBox = h("div", {}), listBox = h("div", { class: "stack" });

  async function decide(t, approve) {
    const note = h("input", { type: "text", placeholder: "Optional note, e.g. vehicle available Tuesday", style: { width: "100%", padding: "7px 10px", border: "1px solid var(--line)", borderRadius: "8px" }, "aria-label": "Note" });
    const ok = await confirmDialog({
      title: approve ? "Approve this transfer?" : "Reject this proposal?",
      body: h("div", { class: "stack" }, h("p", { style: { margin: 0 } }, approve ? `Move ${t.quantity} ${t.unit} of ${t.medicine_name} from ${t.donor_name} to ${t.receiver_name}.` : `Dismiss the proposal to move ${t.quantity} ${t.unit} of ${t.medicine_name} from ${t.donor_name} to ${t.receiver_name}.`),
        approve ? h("div", { class: "callout sim" }, "Demo only: approving updates the simulated inventory. No medicine moves and no message is sent.") : null, note),
      confirmLabel: approve ? "Approve transfer" : "Reject proposal", danger: !approve });
    if (!ok) return;
    try {
      const r = await api.post("/api/redistribution/decision", { id: t.id, decision: approve ? "approve" : "reject", note: note.value || undefined }, { label: `${approve ? "Approval" : "Rejection"} of ${t.medicine_name} transfer` });
      toast(r.queued ? "Decision saved on this device. It will be applied when you are back online." : approve ? "Transfer approved. Simulated stock updated." : "Proposal rejected.", r.queued ? "warn" : "");
      if (r.queued) { data.proposed = data.proposed.filter((p) => p.id !== t.id); st.active = data.proposed[0]?.id || null; draw(); return; }
      app.rerender();
    } catch (e) {
      toast(e.status === 409 || e.status === 404 ? `${e.message} Stock changed since this was proposed; the list has been refreshed.` : e.message, "error");
      app.rerender();
    }
  }

  function draw() {
    mapBox.replaceChildren(renderMap({ points: data.clinics, transfers: data.proposed, activeTransferId: st.active, regionNames, selectedId: st.clinic,
      onSelect: (id) => { const t = data.proposed.find((p) => p.donor_id === id || p.receiver_id === id); if (t) { st.active = t.id; } st.clinic = id; draw(); } }));
    listBox.replaceChildren(...(data.proposed.length ? data.proposed.map(card) : [empty("No transfers to propose", "No clinic has spare unexpired stock that another clinic needs in time.")]));
  }
  function card(t) {
    const on = t.id === st.active;
    return h("div", { class: `item ${t.receiver_risk_before}`, style: on ? { boxShadow: "0 0 0 2px var(--teal)" } : {}, tabindex: 0, onClick: () => { st.active = t.id; draw(); } },
      h("h3", {}, h("b", {}, `${fmt.int(t.quantity)} ${t.unit}`), t.medicine_name), h("div", { class: "row" }, h("b", {}, t.donor_name), "sends to", h("b", {}, t.receiver_name), riskChip(t.receiver_risk_before)),
      h("div", { class: "muted" }, t.rationale),
      h("table", { class: "t" }, h("tbody", {}, [
        ["Receiver stock lasts", `${fmt.days(t.receiver_days_to_stockout_before)} now, ${fmt.days(t.receiver_days_to_stockout_after)} after`],
        ["Journey", `${fmt.num(t.distance_km, 0)} km, about ${fmt.num(t.travel_hours, 1)} h, arrives ${fmt.date(t.arrival_date)}`],
        ["Donor keeps", `${fmt.int(t.donor_stock_after_cover_period)} ${t.unit} after covering its own demand (its minimum is ${fmt.int(t.donor_min_safety_stock)})`],
        ["Stock sent", t.batches.map((b) => `${b.quantity} from batch ${b.batch_id}, expires ${fmt.date(b.expiry_date)}`).join("; ")]].map(([a, b]) => h("tr", {}, h("td", { class: "muted", style: { width: "34%" } }, a), h("td", {}, b))))),
      h("div", { class: "row" }, h("button", { class: "btn primary small", onClick: (e) => { e.stopPropagation(); decide(t, true); } }, "Review and approve"), h("button", { class: "btn small", onClick: (e) => { e.stopPropagation(); decide(t, false); } }, "Reject"), h("span", { class: "chip neutral" }, "Needs human approval")));
  }
  draw();
  const decided = data.decided.length ? h("div", { class: "stack" }, data.decided.map((t) => h("div", { class: `item ${t.status === "approved" ? "low" : "moderate"} done` }, h("h3", {}, h("span", { class: `chip ${t.status === "approved" ? "low" : "neutral"}` }, t.status === "approved" ? "Approved" : "Rejected"), `${fmt.int(t.quantity)} ${t.medicine_name}`),
    h("div", { class: "muted" }, `${t.donor_name} to ${t.receiver_name}, decided ${fmt.dt(t.decided_at)}${t.note ? `. Note: ${t.note}` : ""}`)))) : h("p", { class: "muted" }, "No decisions yet.");
  const units = data.proposed.reduce((a, p) => a + p.quantity, 0);
  return h("div", {}, pageHeader("Redistribution Center", "Spare stock at one clinic can bridge a shortage at another while the next delivery is on its way. These are recommendations; nothing moves until a person approves."),
    cached ? h("div", { class: "callout", style: { marginBottom: "12px" } }, "Offline: showing saved proposals. Approvals are kept on this device and applied when you reconnect, if they still make sense.") : null,
    h("div", { class: "ribbon" }, kpi(data.proposed.length, "Proposals awaiting a decision", { tone: data.proposed.length ? "warn" : "good" }), kpi(fmt.int(units), "Units that could move"), kpi(data.decided.filter((d) => d.status === "approved").length, "Approved this session", { tone: "good" })),
    h("div", { class: "callout", style: { marginBottom: "14px" } }, h("b", {}, "Safety rules applied to every proposal: "), "the same medicine only; expired stock is never sent; at least 45 days of shelf life remain on arrival; the donor keeps its own safety stock even if demand runs high; cold-chain items only go to clinics with cold chain; the stock must arrive before the receiver runs out."),
    h("div", { class: "grid cols-main" }, panel("Transfer map", mapBox, { hint: "Dashed arrows are proposals; the selected one is highlighted." }), h("div", { class: "stack" }, h("h2", {}, "Proposed transfers"), listBox)),
    h("div", { style: { height: "16px" } }), panel("Decisions", decided));
}
