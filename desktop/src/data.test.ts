import { describe, it, expect } from "vitest";
import {
  checkDraft,
  money,
  number,
  samplePositions,
  sampleStages,
} from "./data";
const valid = { topN: 5, interval: 10, risk: 1, leverage: 5 };
describe("bounded unapplied drafts", () => {
  it("accepts valid values", () => expect(checkDraft(valid)).toBe(true));
  for (const [key, value] of [
    ["topN", 0],
    ["topN", 1.5],
    ["topN", 1001],
    ["interval", 0],
    ["interval", Infinity],
    ["risk", NaN],
    ["risk", 1.01],
    ["leverage", 11],
    ["leverage", 0],
    ["risk", "1"],
  ] as const) {
    it(`rejects ${key}=${value}`, () =>
      expect(checkDraft({ ...valid, [key]: value } as typeof valid)).toBe(
        false,
      ));
  }
});
it("renders missing/nonfinite measurements as unavailable, not zero", () => {
  for (const value of [undefined, null, NaN, Infinity]) {
    expect(money(value)).toBe("—");
    expect(number(value)).toBe("—");
  }
  expect(money(0)).toBe("$0.00");
});
it("sample identifiers and measurements stay explicit", () => {
  expect(samplePositions.every((p) => p.trade_id.startsWith("sample-"))).toBe(
    true,
  );
  expect(sampleStages.every((s) => s.latency_sample_count > 0)).toBe(true);
});
