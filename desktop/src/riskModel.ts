import type { Position } from "./data";

/** Illustrative account value used only for the sample workspace (matches the Overview). */
export const DEMO_EQUITY = 128450.25;

/** Illustrative limits for the sample workspace (the documented configuration defaults). */
export const DEMO_CONFIGURATION: Record<string, number> = {
  max_open_positions: 3,
  max_positions_per_symbol: 1,
  max_risk_per_trade_pct: 1,
  max_leverage: 10,
  max_drawdown_pct: 10,
  max_correlated_exposure_pct: 15,
  max_sector_exposure_pct: 20,
};

export type LimitStatus = "ok" | "watch" | "breach" | "unmeasured";
export type LimitUnit = "count" | "pct" | "x";

export type RiskLimits = {
  maxOpenPositions: number | null;
  maxPositionsPerSymbol: number | null;
  maxRiskPerTradePct: number | null;
  maxLeverage: number | null;
  maxDrawdownPct: number | null;
  maxCorrelatedExposurePct: number | null;
  maxSectorExposurePct: number | null;
};
export type SymbolExposure = {
  symbol: string;
  count: number;
  notional: number;
  risk: number;
  pnl: number;
  share: number;
};
export type LimitRow = {
  id: string;
  label: string;
  used: number | null;
  limit: number | null;
  unit: LimitUnit;
  status: LimitStatus;
  note: string;
};
export type RiskSummary = {
  equity: number | null;
  count: number;
  longUsd: number;
  shortUsd: number;
  gross: number;
  net: number;
  riskAtStop: number;
  unrealizedPnl: number;
  unknownPnl: number;
  symbols: SymbolExposure[];
  rows: LimitRow[];
};

const positive = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) && value > 0 ? value : null;
const amount = (value: unknown): number =>
  typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;

/** Read configured risk limits; anything missing or invalid is simply "not configured". */
export function readLimits(configuration: Record<string, unknown>): RiskLimits {
  return {
    maxOpenPositions: positive(configuration.max_open_positions),
    maxPositionsPerSymbol: positive(configuration.max_positions_per_symbol),
    maxRiskPerTradePct: positive(configuration.max_risk_per_trade_pct),
    maxLeverage: positive(configuration.max_leverage),
    maxDrawdownPct: positive(configuration.max_drawdown_pct),
    maxCorrelatedExposurePct: positive(configuration.max_correlated_exposure_pct),
    maxSectorExposurePct: positive(configuration.max_sector_exposure_pct),
  };
}

export function statusFor(used: number | null, limit: number | null): LimitStatus {
  if (used === null || limit === null) return "unmeasured";
  const ratio = used / limit;
  // Exactly at the limit is "at capacity", not a breach; only exceeding it is.
  return ratio > 1 ? "breach" : ratio >= 0.8 ? "watch" : "ok";
}

/**
 * Derive exposure and limit usage from open positions only. Percentage limits need an
 * account value; when none is attached they stay "unmeasured" instead of being guessed.
 */
export function summarizeRisk(
  positions: Position[],
  equity: number | null,
  limits: RiskLimits,
): RiskSummary {
  const bySymbol = new Map<string, SymbolExposure>();
  let longUsd = 0;
  let shortUsd = 0;
  let riskAtStop = 0;
  let unrealizedPnl = 0;
  let unknownPnl = 0;
  let largestRisk = 0;
  for (const p of positions) {
    const notional = amount(p.position_size_usd);
    const risk = amount(p.risk_amount_usd);
    if (/^(short|sell)$/i.test(p.side)) shortUsd += notional;
    else longUsd += notional;
    riskAtStop += risk;
    largestRisk = Math.max(largestRisk, risk);
    if (typeof p.pnl === "number" && Number.isFinite(p.pnl)) unrealizedPnl += p.pnl;
    else unknownPnl += 1;
    const row = bySymbol.get(p.symbol) ?? {
      symbol: p.symbol, count: 0, notional: 0, risk: 0, pnl: 0, share: 0,
    };
    row.count += 1;
    row.notional += notional;
    row.risk += risk;
    if (typeof p.pnl === "number" && Number.isFinite(p.pnl)) row.pnl += p.pnl;
    bySymbol.set(p.symbol, row);
  }
  const gross = longUsd + shortUsd;
  const symbols = [...bySymbol.values()]
    .map((row) => ({ ...row, share: gross > 0 ? row.notional / gross : 0 }))
    .sort((a, b) => b.notional - a.notional);
  const pctOfEquity = (value: number) => (equity ? (value / equity) * 100 : null);
  const needsEquity = "Needs an attached account value";
  const row = (
    id: string, label: string, used: number | null, limit: number | null,
    unit: LimitUnit, note: string,
  ): LimitRow => ({ id, label, used, limit, unit, status: statusFor(used, limit), note });
  const rows: LimitRow[] = [
    row("open", "Open positions", positions.length, limits.maxOpenPositions, "count", "Count of open trades"),
    row("symbol", "Positions per instrument", symbols.reduce((m, s) => Math.max(m, s.count), 0),
      limits.maxPositionsPerSymbol, "count", "Highest count on one instrument"),
    row("trade", "Largest single-trade risk", pctOfEquity(largestRisk), limits.maxRiskPerTradePct, "pct",
      equity ? "Risk at stop ÷ account value" : needsEquity),
    row("leverage", "Gross leverage", equity ? gross / equity : null, limits.maxLeverage, "x",
      equity ? "Gross notional ÷ account value" : needsEquity),
    row("drawdown", "Stop-out loss vs drawdown limit", pctOfEquity(riskAtStop), limits.maxDrawdownPct, "pct",
      equity ? "If every stop fills, loss ÷ account value" : needsEquity),
    row("correlated", "Correlated exposure", null, limits.maxCorrelatedExposurePct, "pct",
      "Needs a correlation model; not measured here"),
    row("sector", "Sector exposure", null, limits.maxSectorExposurePct, "pct",
      "Needs a sector mapping; not measured here"),
  ];
  return {
    equity, count: positions.length, longUsd, shortUsd, gross, net: longUsd - shortUsd,
    riskAtStop, unrealizedPnl, unknownPnl, symbols, rows,
  };
}

export function formatUsed(value: number | null, unit: LimitUnit): string {
  if (value === null) return "—";
  if (unit === "count") return String(Math.round(value));
  if (unit === "x") return `${value.toFixed(2)}×`;
  return `${value.toFixed(2)}%`;
}
