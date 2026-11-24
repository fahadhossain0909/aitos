import { describe, expect, it } from "vitest";
import type { Position } from "./data";
import { readLimits, statusFor, summarizeRisk } from "./riskModel";

const pos = (over: Partial<Position>): Position => ({
  trade_id: "t", symbol: "BTCUSDT", side: "LONG", quantity: 1, entry_price: 1,
  position_size_usd: 1000, risk_amount_usd: 10, pnl: 5, state: "position_opened",
  entry_time: "2026-11-07T08:42:00Z", ...over,
});
const limits = readLimits({
  max_open_positions: 3, max_positions_per_symbol: 1, max_risk_per_trade_pct: 1,
  max_leverage: 10, max_drawdown_pct: 10, max_correlated_exposure_pct: 15, max_sector_exposure_pct: 20,
});

describe("risk model", () => {
  it("ignores missing, non-finite and non-positive configured limits", () => {
    const parsed = readLimits({ max_open_positions: "3", max_leverage: NaN, max_drawdown_pct: -1 });
    expect(Object.values(parsed).every((v) => v === null)).toBe(true);
  });
  it("classifies usage against a limit", () => {
    expect(statusFor(1, 4)).toBe("ok");
    expect(statusFor(8, 10)).toBe("watch");
    expect(statusFor(10, 10)).toBe("watch");
    expect(statusFor(11, 10)).toBe("breach");
    expect(statusFor(null, 10)).toBe("unmeasured");
    expect(statusFor(2, null)).toBe("unmeasured");
  });
  it("derives exposure from open positions and splits long/short", () => {
    const s = summarizeRisk(
      [pos({}), pos({ symbol: "ETHUSDT", side: "SHORT", position_size_usd: 500, risk_amount_usd: 20, pnl: null })],
      null, limits,
    );
    expect(s.gross).toBe(1500);
    expect(s.net).toBe(500);
    expect(s.riskAtStop).toBe(30);
    expect(s.unknownPnl).toBe(1);
    expect(s.symbols[0].symbol).toBe("BTCUSDT");
    expect(s.symbols[0].share).toBeCloseTo(2 / 3);
  });
  it("never guesses percentage limits without an account value", () => {
    const s = summarizeRisk([pos({})], null, limits);
    const byId = Object.fromEntries(s.rows.map((r) => [r.id, r]));
    expect(byId.open.status).toBe("ok");
    expect(byId.trade.status).toBe("unmeasured");
    expect(byId.leverage.used).toBeNull();
    expect(byId.correlated.status).toBe("unmeasured");
  });
  it("flags a breach once account value is known", () => {
    const s = summarizeRisk([pos({ position_size_usd: 120000, risk_amount_usd: 400 })], 10000, limits);
    const byId = Object.fromEntries(s.rows.map((r) => [r.id, r]));
    expect(byId.leverage.used).toBeCloseTo(12); // 120000 / 10000
    expect(byId.leverage.status).toBe("breach");
    expect(byId.trade.status).toBe("breach"); // 400 / 10000 = 4% of a 1% limit
  });
  it("hardens against malformed numbers", () => {
    const s = summarizeRisk([pos({ position_size_usd: NaN, risk_amount_usd: -5 })], 1000, limits);
    expect(s.gross).toBe(0);
    expect(s.riskAtStop).toBe(0);
  });
});
