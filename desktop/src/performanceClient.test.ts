import { it, expect } from "vitest";
import fixture from "./performance.fixture.json";
import { parsePerformance } from "./performanceClient";
const now = Date.parse(fixture.generated_at);
it("accepts the actual Python analytics output schema", () =>
  expect(parsePerformance(fixture, now).summary.net_pnl_usd).toBe(10));
for (const [key, value] of [
  ["available", false],
  ["scope", "account_equity"],
  ["source", "sample"],
  ["evidence_sha256", "fake"],
  ["loaded_trades", -1],
  ["generated_at", "invalid"],
] as const) {
  it(`rejects malformed ${key}`, () =>
    expect(() =>
      parsePerformance({ ...fixture, [key]: value }, now),
    ).toThrow());
}
it("rejects invalid financial summaries", () =>
  expect(() =>
    parsePerformance(
      { ...fixture, summary: { ...fixture.summary, expectancy_usd: NaN } },
      now,
    ),
  ).toThrow());
it("rejects stale financial evidence", () =>
  expect(() => parsePerformance(fixture, now + 16000)).toThrow());
it("rejects nonmonotonic observations", () =>
  expect(() =>
    parsePerformance(
      {
        ...fixture,
        loaded_trades: 2,
        summary: { ...fixture.summary, trades: 2, wins: 2 },
        directions: {
          ...fixture.directions,
          LONG: { ...fixture.directions.LONG, trades: 2, wins: 2 },
        },
        curve: [...fixture.curve, ...fixture.curve],
      },
      now,
    ),
  ).toThrow());
it("rejects pretending unknown cost coverage is a known total", () =>
  expect(() =>
    parsePerformance(
      {
        ...fixture,
        costs: {
          ...fixture.costs,
          fees_paid_usd: { observed_rows: 0, total_rows: 1, total_usd: 0 },
        },
      },
      now,
    ),
  ).toThrow());
it("rejects a curve that contradicts the recorded summary", () => {
  const p = structuredClone(fixture);
  p.curve[0].net_pnl_usd = 1234;
  expect(() => parsePerformance(p, now)).toThrow();
});
it("rejects negative drawdown evidence", () =>
  expect(() =>
    parsePerformance(
      {
        ...fixture,
        summary: { ...fixture.summary, max_closed_pnl_drawdown_usd: -1 },
      },
      now,
    ),
  ).toThrow());
it("rejects inconsistent strategy counts", () =>
  expect(() =>
    parsePerformance({ ...fixture, strategies: {} }, now),
  ).toThrow());
