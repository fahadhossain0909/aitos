export type AccountObservation = {
  schema_version: 1;
  source: "binance_signed_rest";
  observed_at: string;
  controls: "read_only";
  consistent_snapshot: false;
  totals: Record<string, string>;
  positions: { symbol: string; side: string; quantity: string; markPrice: string; entryPrice: string; unRealizedProfit: string }[];
  limitations: string[];
};
const money = (s: unknown): s is string => typeof s === "string" && /^-?(?:0|[1-9][0-9]{0,23})(?:\.[0-9]{1,18})?$/.test(s);
const obj = (s: unknown): s is Record<string, unknown> => !!s && typeof s === "object" && !Array.isArray(s);
export function parseAccountObservation(input: unknown, now = Date.now()): AccountObservation {
  if (!obj(input) || input.schema_version !== 1 || input.source !== "binance_signed_rest" || input.controls !== "read_only" || input.consistent_snapshot !== false || typeof input.observed_at !== "string" || !/(Z|[+-]\d{2}:\d{2})$/.test(input.observed_at)) throw Error("Invalid account observation");
  const age = now - Date.parse(input.observed_at);
  if (!Number.isFinite(age) || age < -5000 || age > 30000) throw Error("Stale account observation");
  const fields = ["totalWalletBalance", "totalUnrealizedProfit", "totalMarginBalance", "totalInitialMargin", "totalMaintMargin", "availableBalance"];
  if (!obj(input.totals) || Object.keys(input.totals).length !== fields.length || !fields.every(k => money((input.totals as Record<string, unknown>)[k])) || !Array.isArray(input.positions) || input.positions.length > 2000 || !Array.isArray(input.limitations) || input.limitations.length > 20 || !input.limitations.every(s => typeof s === "string" && s.length <= 120)) throw Error("Invalid account values");
  const seen = new Set<string>();
  for (const p of input.positions) {
    if (!obj(p) || typeof p.symbol !== "string" || !/^[A-Z0-9]{2,24}$/.test(p.symbol) || !["BOTH", "LONG", "SHORT"].includes(String(p.side)) || !["quantity", "markPrice", "entryPrice", "unRealizedProfit"].every(k => money(p[k]))) throw Error("Invalid account position");
    const key = p.symbol + ":" + p.side;
    if (seen.has(key)) throw Error("Duplicate account position");
    seen.add(key);
  }
  // Preserve decimal strings: no float rounding, inferred realized PnL or zero fallback.
  return input as unknown as AccountObservation;
}
