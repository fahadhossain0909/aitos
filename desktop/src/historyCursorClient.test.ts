import { describe, it, expect } from "vitest";
import { parseHistoryCut } from "./historyCursorClient";
import { samplePositions } from "./data";
const now = Date.parse("2026-11-06T12:00:00Z");
const valid = () => ({
  schema_version: 1,
  source: "materialized_ledger_query",
  consistent_pages: true,
  available: true,
  snapshot_id: "a".repeat(32),
  captured_at: now / 1000,
  expires_at: now / 1000 + 900,
  total_captured: 3,
  truncated: false,
  rows: samplePositions,
  page_cursor: "signed_current",
  next_cursor: null,
});
describe("materialized history", () => {
  it("accepts fixed bounded cut", () =>
    expect(parseHistoryCut(valid(), now).rows.length).toBe(3));
  it("rejects expiry, arbitrary URLs and source substitution", () => {
    expect(() => parseHistoryCut(valid(), now + 901000)).toThrow();
    expect(() =>
      parseHistoryCut({ ...valid(), next_cursor: "https://evil" }, now),
    ).toThrow();
    expect(() =>
      parseHistoryCut({ ...valid(), source: "sample" }, now),
    ).toThrow();
  });
  it("rejects inconsistent row counts or malformed positions", () => {
    expect(() =>
      parseHistoryCut({ ...valid(), total_captured: 1 }, now),
    ).toThrow();
    expect(() => parseHistoryCut({ ...valid(), rows: [{}] }, now)).toThrow();
  });
});
