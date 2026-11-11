import { it, expect } from "vitest";
import fixture from "./research.fixture.json";
import { parsePlan } from "./ResearchPlanner";
it("accepts actual Python partition-planner output", () =>
  expect(parsePlan(fixture).fold_count).toBeGreaterThan(1));
it("rejects a fake promotion authority", () =>
  expect(() =>
    parsePlan({ ...fixture, promotion_authorized: true }),
  ).toThrow());
it("rejects a fabricated successful backtest", () =>
  expect(() => parsePlan({ ...fixture, backtest_executed: true })).toThrow());
it("rejects invented validated status", () =>
  expect(() => parsePlan({ ...fixture, status: "validated" })).toThrow());
it("rejects invalid fingerprint", () =>
  expect(() => parsePlan({ ...fixture, evidence_sha256: "fake" })).toThrow());
it("rejects broken fold-count metadata", () =>
  expect(() => parsePlan({ ...fixture, fold_count: 99 })).toThrow());
it("rejects a gap smaller than declared", () => {
  const p = structuredClone(fixture);
  p.folds[0].train_end = p.folds[0].test_start;
  expect(() => parsePlan(p)).toThrow();
});
it("rejects test overlap with locked holdout", () => {
  const p = structuredClone(fixture);
  p.folds[p.folds.length - 1].test_end = p.holdout.end;
  expect(() => parsePlan(p)).toThrow();
});
it("rejects invalid calendar dates", () => {
  const p = structuredClone(fixture);
  p.folds[0].train_start = "2025-02-30";
  expect(() => parsePlan(p)).toThrow();
});
