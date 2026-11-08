export type PerformanceSummary = {
  trades: number;
  wins: number;
  losses: number;
  breakeven: number;
  net_pnl_usd: number | null;
  win_rate: number | null;
  expectancy_usd: number | null;
  median_pnl_usd: number | null;
  profit_factor: number | null;
  profit_factor_reason: string | null;
  best_trade_usd: number | null;
  worst_trade_usd: number | null;
  max_closed_pnl_drawdown_usd?: number | null;
};
export type PerformanceReport = {
  schema_version: 1;
  available: true;
  source: "clickhouse_closed_ledger";
  scope: "bounded_recorded_net_pnl_not_account_equity";
  selection: "latest_ledger_updates";
  generated_at: string;
  evidence_sha256: string;
  window_limit: number;
  loaded_trades: number;
  truncated: boolean;
  start: string | null;
  end: string | null;
  summary: PerformanceSummary;
  curve: { at: string; net_pnl_usd: number; drawdown_usd: number }[];
  costs: Record<
    "fees_paid_usd" | "funding_pnl_usd",
    { observed_rows: number; total_rows: number; total_usd: number | null }
  >;
  directions: Record<"LONG" | "SHORT", PerformanceSummary>;
  strategies: Record<string, PerformanceSummary>;
  limitations: string[];
};
const object = (x: unknown): x is Record<string, unknown> =>
  x !== null && typeof x === "object" && !Array.isArray(x);
const finite = (x: unknown): x is number =>
  typeof x === "number" && Number.isFinite(x);
const count = (x: unknown): x is number =>
  finite(x) && Number.isInteger(x) && x >= 0;
const nullable = (x: unknown) => x === null || finite(x);
const timestamp = (x: unknown): x is string =>
  typeof x === "string" &&
  /(Z|[+-]\d{2}:\d{2})$/.test(x) &&
  Number.isFinite(Date.parse(x));
function summary(x: unknown): x is PerformanceSummary {
  return (
    object(x) &&
    ["trades", "wins", "losses", "breakeven"].every((k) => count(x[k])) &&
    x.trades ===
      (x.wins as number) + (x.losses as number) + (x.breakeven as number) &&
    [
      "net_pnl_usd",
      "win_rate",
      "expectancy_usd",
      "median_pnl_usd",
      "profit_factor",
      "best_trade_usd",
      "worst_trade_usd",
    ].every((k) => nullable(x[k])) &&
    (x.win_rate === null ||
      (finite(x.win_rate) && x.win_rate >= 0 && x.win_rate <= 1)) &&
    (x.profit_factor === null ||
      (finite(x.profit_factor) && x.profit_factor >= 0)) &&
    (x.profit_factor_reason === null ||
      typeof x.profit_factor_reason === "string")
  );
}
export function parsePerformance(
  x: unknown,
  now = Date.now(),
): PerformanceReport {
  if (
    !object(x) ||
    x.schema_version !== 1 ||
    x.available !== true ||
    x.source !== "clickhouse_closed_ledger" ||
    x.scope !== "bounded_recorded_net_pnl_not_account_equity" ||
    x.selection !== "latest_ledger_updates" ||
    !timestamp(x.generated_at) ||
    Math.abs(now - Date.parse(x.generated_at)) > 15000 ||
    typeof x.evidence_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(x.evidence_sha256) ||
    !count(x.window_limit) ||
    x.window_limit < 1 ||
    x.window_limit > 10000 ||
    !count(x.loaded_trades) ||
    x.loaded_trades > x.window_limit ||
    typeof x.truncated !== "boolean" ||
    !summary(x.summary) ||
    x.summary.trades !== x.loaded_trades ||
    !nullable(x.summary.max_closed_pnl_drawdown_usd) ||
    (finite(x.summary.max_closed_pnl_drawdown_usd) &&
      x.summary.max_closed_pnl_drawdown_usd < 0) ||
    !Array.isArray(x.curve) ||
    x.curve.length > x.loaded_trades ||
    !x.curve.every(
      (p) =>
        object(p) &&
        timestamp(p.at) &&
        finite(p.net_pnl_usd) &&
        finite(p.drawdown_usd) &&
        p.drawdown_usd >= 0,
    ) ||
    !object(x.costs) ||
    !object(x.directions) ||
    !summary(x.directions.LONG) ||
    !summary(x.directions.SHORT) ||
    x.directions.LONG.trades + x.directions.SHORT.trades !== x.loaded_trades ||
    !object(x.strategies) ||
    !Object.values(x.strategies).every(summary) ||
    !Array.isArray(x.limitations) ||
    !x.limitations.every((s) => typeof s === "string")
  )
    throw Error("Invalid performance evidence");
  const loaded = x.loaded_trades,
    curve = x.curve;
  if (
    !["fees_paid_usd", "funding_pnl_usd"].every((k) => {
      const cost = (x.costs as Record<string, unknown>)[k];
      return (
        object(cost) &&
        count(cost.observed_rows) &&
        cost.total_rows === loaded &&
        cost.observed_rows <= loaded &&
        nullable(cost.total_usd) &&
        (cost.observed_rows === loaded || cost.total_usd === null)
      );
    })
  )
    throw Error("Invalid cost coverage");
  if (
    x.curve.some(
      (p, i) => i > 0 && Date.parse(p.at) <= Date.parse(curve[i - 1].at),
    )
  )
    throw Error("Nonmonotonic performance curve");
  if (
    (x.loaded_trades > 0 &&
      (!timestamp(x.start) || !timestamp(x.end) || !x.curve.length)) ||
    (x.loaded_trades === 0 &&
      (x.start !== null || x.end !== null || x.curve.length !== 0))
  )
    throw Error("Invalid performance window");
  const strategies = Object.values(x.strategies) as PerformanceSummary[];
  if (strategies.reduce((n, s) => n + s.trades, 0) !== loaded)
    throw Error("Invalid strategy sample counts");
  if (loaded > 0) {
    const final = curve[curve.length - 1];
    if (
      x.start !== curve[0].at ||
      x.end !== final.at ||
      !finite(x.summary.net_pnl_usd) ||
      Math.abs(final.net_pnl_usd - x.summary.net_pnl_usd) >
        1e-8 * Math.max(1, Math.abs(x.summary.net_pnl_usd))
    )
      throw Error("Curve and summary evidence disagree");
  }
  return x as PerformanceReport;
}
