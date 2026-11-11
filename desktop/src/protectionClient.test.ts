import { describe, it, expect } from "vitest";
import { parseProtectionObservation } from "./protectionClient";
const now = Date.parse("2026-11-06T12:00:00Z");
const valid = () => ({
  schema_version: 1,
  source: "caller_exported_protection_inventory",
  observed_at: new Date(now).toISOString(),
  captured_at: new Date(now).toISOString(),
  controls: "read_only",
  safe_to_resume: false,
  account_exposure_verified: false,
  automatic_actions: [],
  counts: {
    unowned_order: 0,
    mismatch: 0,
    not_in_open_inventory: 1,
    unapplied_match: 0,
    applied_order_present: 0,
  },
  findings: [
    {
      id: "a".repeat(24),
      symbol: "BTCUSDT",
      status: "not_in_open_inventory",
      reasons: ["query terminal evidence"],
    },
  ],
  limitations: ["export_provenance_not_authenticated"],
});
describe("protection evidence", () => {
  it("accepts bounded read-only evidence", () =>
    expect(parseProtectionObservation(valid(), now).safe_to_resume).toBe(
      false,
    ));
  for (const key of ["safe_to_resume", "account_exposure_verified"])
    it(`rejects ${key} grant`, () =>
      expect(() =>
        parseProtectionObservation({ ...valid(), [key]: true }, now),
      ).toThrow());
  it("rejects stale/future capture and actions", () => {
    expect(() =>
      parseProtectionObservation(
        { ...valid(), captured_at: "2020-01-01T00:00:00Z" },
        now,
      ),
    ).toThrow();
    expect(() => parseProtectionObservation(valid(), now - 1)).toThrow();
    expect(() =>
      parseProtectionObservation(
        { ...valid(), automatic_actions: ["replace"] },
        now,
      ),
    ).toThrow();
  });
  it("rejects forged count and duplicate identities", () => {
    const s = valid();
    s.counts.not_in_open_inventory = 0;
    expect(() => parseProtectionObservation(s, now)).toThrow();
    const t = valid();
    t.findings.push(t.findings[0]);
    expect(() => parseProtectionObservation(t, now)).toThrow();
  });
});
