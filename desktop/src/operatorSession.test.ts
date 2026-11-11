import { it, expect } from "vitest";
import { parseOperatorSession } from "./operatorSession";
const valid = {
  schema_version: 1,
  gateway: true,
  authenticated: true,
  expires_in_seconds: 900,
  controls: "read_only",
};
it("accepts a bounded read-only session", () =>
  expect(parseOperatorSession(valid).authenticated).toBe(true));
for (const [key, value] of [
  ["gateway", false],
  ["controls", "live"],
  ["expires_in_seconds", NaN],
  ["expires_in_seconds", 3601],
  ["expires_in_seconds", 0],
  ["authenticated", "true"],
  ["schema_version", 2],
] as const) {
  it(`rejects invalid ${key}`, () =>
    expect(() => parseOperatorSession({ ...valid, [key]: value })).toThrow());
}
it("accepts explicit unauthenticated status", () =>
  expect(
    parseOperatorSession({
      ...valid,
      authenticated: false,
      expires_in_seconds: 0,
    }).authenticated,
  ).toBe(false));
it("rejects a supposedly signed-out session with remaining authority", () =>
  expect(() =>
    parseOperatorSession({ ...valid, authenticated: false }),
  ).toThrow());
