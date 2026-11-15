// Small bits shared by several pages.
import { fmt } from "../lib/ui.js";

export function signalLine(s) {
  if (!s) return "";
  const dir = s.ratio_vs_seasonal >= 1.15 ? "above" : s.ratio_vs_seasonal <= 0.85 ? "below" : "close to";
  return `Expected ${fmt.num(s.expected_avg, 1)} cases a week, ${dir} the usual ${fmt.num(s.seasonal_avg, 1)} for this season. Peak week of ${fmt.date(s.peak_week)}.`;
}
export const DISEASE_COLOR = { malaria: "var(--blue)", cholera: "var(--teal)", pneumonia: "#6b5bd6" };
