import { describe, it, expect } from "vitest";
import { parseSnapshot, parseHistory } from "./consoleClient";
import { samplePositions, sampleStages } from "./data";
const now = Date.parse("2026-11-07T12:00:00Z");
const valid = () => ({
  schema_version: 1,
  connected: true,
  source: "runtime",
  mode: "paper",
  generated_at: new Date(now).toISOString(),
  controls: "read_only",
  history_available: true,
  configuration: {},
  positions: structuredClone(samplePositions),
  pipeline: structuredClone(sampleStages),
  modules: [{ name: "lifecycle", status: "healthy" }],
});
describe("untrusted console response boundary", () => {
  it("accepts a fresh well-formed snapshot", () =>
    expect(parseSnapshot(valid(), now).positions.length).toBe(3));
  for (const [key, value] of [
    ["schema_version", 2],
    ["connected", "true"],
    ["controls", "writable"],
    ["configuration", null],
    ["modules", [{}]],
    ["positions", [{}]],
    ["pipeline", [{}]],
  ] as const) {
    it(`rejects malformed ${key}`, () =>
      expect(() => parseSnapshot({ ...valid(), [key]: value }, now)).toThrow());
  }
  it("rejects nonfinite financial values", () => {
    const s = valid();
    s.positions[0].entry_price = NaN;
    expect(() => parseSnapshot(s, now)).toThrow();
  });
  it("rejects malformed order direction", () => {
    const s = valid();
    s.positions[0].side = "SIDEWAYS";
    expect(() => parseSnapshot(s, now)).toThrow();
  });
  it("rejects stale and future snapshots", () => {
    for (const offset of [-16000, 6000])
      expect(() =>
        parseSnapshot(
          { ...valid(), generated_at: new Date(now + offset).toISOString() },
          now,
        ),
      ).toThrow();
  });
  it("rejects timezone-less timestamps", () =>
    expect(() =>
      parseSnapshot({ ...valid(), generated_at: "2026-11-07T12:00:00" }, now),
    ).toThrow());
  it("drops malformed readiness instead of presenting a fake certificate", () =>
    expect(
      parseSnapshot({ ...valid(), readiness: { live_authorized: true } }, now)
        .readiness,
    ).toBeUndefined());
  it("accepts bounded, explicitly sourced history", () =>
    expect(
      parseHistory({
        available: true,
        source: "clickhouse",
        rows: samplePositions,
      }),
    ).toHaveLength(3));
  it("rejects unsourced or malformed history", () => {
    for (const response of [
      { available: true, rows: samplePositions },
      { available: false, source: "clickhouse", rows: [] },
      { available: true, source: "clickhouse", rows: [{}] },
      {
        available: true,
        source: "clickhouse",
        rows: Array(101).fill(samplePositions[0]),
      },
    ])
      expect(() => parseHistory(response)).toThrow();
  });
});
