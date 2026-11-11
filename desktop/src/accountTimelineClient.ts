export type TimelinePage = { schema_version: 1; source: "durable_account_observations"; consistent_insert_cut: true; upper_id: number; rows: {id:number; observed_at:string; totals:Record<string,string>}[]; next_cursor:string|null; limitations:string[] };
export function parseTimelinePage(input: unknown): TimelinePage {
  const s = input as TimelinePage | null;
  if (!s || s.schema_version !== 1 || s.source !== "durable_account_observations" || s.consistent_insert_cut !== true || !Number.isSafeInteger(s.upper_id) || s.upper_id < 0 || !Array.isArray(s.rows) || s.rows.length > 100 || (s.next_cursor !== null && (typeof s.next_cursor !== "string" || !/^[A-Za-z0-9_-]{1,512}$/.test(s.next_cursor))) || !Array.isArray(s.limitations) || !s.limitations.every(v => typeof v === "string" && v.length <= 120)) throw Error("Invalid account timeline");
  let previous = s.upper_id + 1;
  const fields = ["totalWalletBalance","totalMarginBalance","totalUnrealizedProfit","totalInitialMargin","totalMaintMargin","availableBalance"];
  for (const row of s.rows) {
    if (!row || !Number.isSafeInteger(row.id) || row.id < 1 || row.id >= previous || typeof row.observed_at !== "string" || !/(Z|[+-]\d{2}:\d{2})$/.test(row.observed_at) || !Number.isFinite(Date.parse(row.observed_at)) || !row.totals || Object.keys(row.totals).length !== fields.length || !fields.every(k => typeof row.totals[k] === "string" && /^-?(?:0|[1-9][0-9]{0,23})(?:\.[0-9]{1,18})?$/.test(row.totals[k]))) throw Error("Invalid account timeline row");
    previous = row.id;
  }
  if (!s.rows.length && s.next_cursor !== null) throw Error("Invalid empty continuation");
  return s;
}
