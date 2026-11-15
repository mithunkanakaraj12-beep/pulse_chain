import { h, fmt, panel, pageHeader, riskChip, toast, confirmDialog, empty, seg, kpi, RISK_ORDER } from "../lib/ui.js";
import * as api from "../lib/api.js";

const TYPE = { critical_shortage: "Critical shortage", upcoming_shortage: "Shortage ahead", disease_surge: "Disease surge", replenishment: "Resupply due" };

export async function render({ meta, app }) {
  const [al, log, settings] = await Promise.all([api.get("/api/alerts"), api.get("/api/sms/log"), api.get("/api/settings")]);
  const st = { sev: "all", showAck: false };
  const listBox = h("div", { class: "stack" });
  const sms = settings.sms;

  async function ack(a) {
    try { const r = await api.post("/api/alerts/acknowledge", { key: a.key }, { label: "Alert acknowledgement" }); toast(r.queued ? "Saved on this device; will sync when online." : "Alert acknowledged.", r.queued ? "warn" : ""); a.acknowledged = true; draw(); app.refreshMeta().catch(() => {}); } catch (e) { toast(e.message, "error"); }
  }
  async function send(a, live) {
    const ok = await confirmDialog({
      title: live ? "Send a real text message?" : "Simulate this text message?",
      body: h("div", { class: "stack" }, h("div", { class: "sms-bubble" }, a.sms_preview), h("div", { class: "muted" }, `To ${a.health_worker}, ${a.phone}`),
        live ? h("div", { class: "callout warn" }, "This will send a real SMS through Twilio and may cost money. Only continue if this number is yours to message.") : h("div", { class: "callout sim" }, "Simulation: the message is written to the log below. Nothing is sent to any phone.")),
      confirmLabel: live ? "Send real SMS" : "Simulate send", danger: live });
    if (!ok) return;
    try { const r = await api.post("/api/sms/send", { alert_key: a.key, mode: live ? "live" : "simulated", confirm: live }); toast(live ? "Message sent." : "Simulated SMS added to the log."); app.rerender(); } catch (e) { toast(e.message, "error"); }
  }
  function draw() {
    const rows = al.alerts.filter((a) => (st.sev === "all" || a.severity === st.sev) && (st.showAck || !a.acknowledged)).sort((a, b) => RISK_ORDER[b.severity] - RISK_ORDER[a.severity] || (a.days_to_stockout ?? 999) - (b.days_to_stockout ?? 999));
    listBox.replaceChildren(...(rows.length ? rows.map((a) => h("div", { class: `item ${a.severity} ${a.acknowledged ? "done" : ""}` },
      h("h3", {}, riskChip(a.severity), a.title, h("span", { class: "chip neutral" }, TYPE[a.type] || a.type)), h("div", {}, h("a", { href: `#/clinics/${a.clinic_id}` }, a.clinic_name), `: ${a.message}`),
      a.recommendation ? h("div", {}, h("b", {}, a.recommendation)) : null,
      a.sms_preview ? h("div", {}, h("div", { class: "sms-bubble" }, a.sms_preview), h("div", { class: "sms-meta" }, `Text for ${a.health_worker} (${a.phone}) - ${a.sms_preview.length} of 160 characters${a.sms_eligible ? "" : " - below the alert level that triggers texts"}${a.last_sms ? ` - last ${a.last_sms.status} ${fmt.dt(a.last_sms.created_at)}` : ""}`)) : null,
      h("div", { class: "row" }, a.sms_preview ? h("button", { class: "btn small primary", onClick: () => send(a, false) }, "Simulate SMS") : null,
        a.sms_preview && sms.live_available ? h("button", { class: "btn small danger", onClick: () => send(a, true) }, "Send real SMS") : null,
        a.acknowledged ? h("span", { class: "chip neutral" }, "Acknowledged") : h("button", { class: "btn small", onClick: () => ack(a) }, "Acknowledge"))))
      : [empty("No alerts here", "Nothing matches this filter. Try All, or show acknowledged alerts.")]));
  }
  draw();
  const thr = h("select", { id: "thr", onChange: async (e) => { try { const r = await api.put("/api/settings", { alert_threshold: e.target.value }, { label: "Alert level change" }); toast(r.queued ? "Saved on this device." : "Alert level updated."); app.rerender(); } catch (er) { toast(er.message, "error"); } } },
    [["moderate", "Watch and above"], ["high", "High and above"], ["critical", "Critical only"]].map(([v, t]) => h("option", { value: v, selected: settings.alert_threshold === v }, t)));
  const logBox = log.messages.length ? h("div", { class: "stack" }, log.messages.slice(0, 12).map((m) => h("div", {}, h("div", { class: "sms-bubble" }, m.body), h("div", { class: "sms-meta" }, `${m.mode === "live" ? "Real" : "Simulated"} - ${m.status} - to ${m.to_number} - ${fmt.dt(m.created_at)}${m.error ? ` - ${m.error}` : ""}`)))) : h("p", { class: "muted" }, "No messages yet. Use Simulate SMS on an alert to see one appear here.");
  return h("div", {}, pageHeader("Alerts Center", "What needs attention, why, and the text message a health worker would receive. Messages are simulated unless live sending is deliberately enabled on the server."),
    h("div", { class: "ribbon" }, kpi(al.active, "Open alerts", { tone: al.active ? "warn" : "good" }), Object.entries(al.counts).map(([k, v]) => kpi(v, TYPE[k] || k)), kpi(sms.mode === "live" ? "Live" : "Simulated", "SMS mode", { tone: sms.mode === "live" ? "danger" : "", detail: sms.live_available ? "Each real send asks for confirmation" : "Real sending is off" })),
    h("div", { class: "grid cols-main" }, h("div", { class: "stack" }, h("div", { class: "row between" }, seg([["all", "All"], ["critical", "Critical"], ["high", "High"], ["moderate", "Watch"]], "all", (v) => { st.sev = v; draw(); }, "Severity"),
      h("label", { class: "chk" }, h("input", { type: "checkbox", onChange: (e) => { st.showAck = e.target.checked; draw(); } }), "Show acknowledged")), listBox),
      h("div", { class: "stack" }, panel("Text alert level", h("div", { class: "field" }, h("label", { for: "thr" }, "Offer an SMS for alerts at or above"), thr), { hint: "Lower levels still appear here; they just are not offered as texts." }),
        panel("Message log", logBox, { hint: sms.live_available ? "Real and simulated messages." : "Live sending is disabled. Enable it on the server (see README) to use Twilio." }))));
}
