import { describe, it, expect } from "vitest";
import { parseProjection } from "./projectionClient";
import { parseSnapshot } from "./consoleClient";
import fixture from "./projection.fixture.json";
const now = Date.parse(fixture.observed_at);
const valid = () => structuredClone(fixture) as Record<string, any>;
describe("projection evidence boundary", () => {
  it("accepts real Python producer fixture and strips extras", () => {
    const report = valid();
    report.secret = "never export";
    expect(parseProjection(report, now)).toEqual(fixture);
  });
  for (const [key, value] of [
    ["live_authorized", true],
    ["production_certified", true],
    ["available", "true"],
    ["status", ["verified"]],
    ["reason", ["none"]],
    ["status", "empty"],
    ["source", "illustrative"],
    ["observed_at", "2026-02-30T12:00:00Z"],
    ["observed_at", "2026-11-06T12:00:00"],
    ["counts", { total: 12, pending: -1, delivered: 13 }],
    ["counts", { total: 12, pending: 3, delivered: 99 }],
    [
      "counts",
      {
        total: Number.MAX_SAFE_INTEGER + 1,
        pending: 0,
        delivered: Number.MAX_SAFE_INTEGER + 1,
      },
    ],
    ["counts", { total: 12, pending: 3, delivered: 9, private: "secret" }],
    ["last_batch", { checked: 4, inserted: 5, acknowledged: 4 }],
    ["last_batch", { checked: 4, inserted: 3, acknowledged: 3 }],
    ["last_batch", { checked: 101, inserted: 101, acknowledged: 101 }],
  ])
    it(`rejects malformed ${key} ${JSON.stringify(value)}`, () => {
      const report = valid();
      report[key as string] = value;
      expect(parseProjection(report, now)).toBeNull();
    });
  it("clears expired/future observations instead of retaining counts", () => {
    expect(parseProjection(valid(), now + 60001)).toBeNull();
    expect(parseProjection(valid(), now - 5001)).toBeNull();
  });
  it("unavailable is not zero pending", () => {
    const report = {
      ...valid(),
      available: false,
      status: "unavailable",
      reason: "not_attached",
      source: "unavailable",
      observed_at: null,
      counts: null,
      last_batch: null,
    };
    expect(parseProjection(report, now)?.counts).toBeNull();
    report.counts = { total: 0, pending: 0, delivered: 0 } as any;
    expect(parseProjection(report, now)).toBeNull();
  });
  it("drops malformed optional evidence without accepting a financial sample fallback", () => {
    const snapshot = {
      schema_version: 1,
      connected: false,
      source: "unattached",
      mode: "unknown",
      generated_at: new Date(now).toISOString(),
      controls: "read_only",
      history_available: false,
      configuration: {},
      positions: [],
      modules: [],
      pipeline: [],
      projection: {
        ...valid(),
        counts: { total: 1, pending: 0, delivered: 99 },
      },
    };
    const parsed = parseSnapshot(snapshot, now);
    expect(parsed.projection).toBeUndefined();
    expect(parsed.positions).toEqual([]);
    expect(parsed.connected).toBe(false);
  });
});
