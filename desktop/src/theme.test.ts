import { describe, expect, it } from "vitest";
import { parseTextSize, parseTheme, resolveTheme, THEMES } from "./theme";

describe("appearance helpers", () => {
  it("keeps the original olive theme for missing or corrupt values", () => {
    for (const bad of [null, undefined, "", "neon", 7, {}]) expect(parseTheme(bad)).toBe("olive");
  });
  it("accepts every declared theme", () => {
    for (const t of THEMES) expect(parseTheme(t)).toBe(t);
  });
  it("only accepts bounded text sizes", () => {
    expect(parseTextSize("115")).toBe("115");
    expect(parseTextSize("400")).toBe("100");
    expect(parseTextSize(null)).toBe("100");
  });
  it("resolves system preference without changing explicit choices", () => {
    expect(resolveTheme("system", true)).toBe("ivory");
    expect(resolveTheme("system", false)).toBe("olive");
    expect(resolveTheme("terminal", true)).toBe("terminal");
  });
});
