import { describe, expect, it } from "vitest";
import {
  activateWorkspace,
  DEFAULT_LAYOUT,
  movePanel,
  parseWorkspaceLayout,
  togglePanel,
} from "./workspaceModel";

describe("workspace layout contract", () => {
  it("returns an isolated default layout for invalid input", () => {
    const first = parseWorkspaceLayout(null);
    first.panels.trading.pop();
    expect(parseWorkspaceLayout({ version: 99 })).toEqual(DEFAULT_LAYOUT);
    expect(DEFAULT_LAYOUT.panels.trading).toContain("copilot");
  });

  it("validates workspace and panel identifiers and removes duplicates", () => {
    const parsed = parseWorkspaceLayout({
      version: 1,
      activeWorkspace: "ai",
      panels: { trading: ["charts", "charts", "unsafe", 4], ai: ["copilot"] },
    });
    expect(parsed.activeWorkspace).toBe("ai");
    expect(parsed.panels.trading).toEqual(["charts"]);
    expect(parsed.panels.ai).toEqual(["copilot"]);
    expect(parsed.panels.quant).toEqual([]);
  });

  it("toggles panels without mutating the original layout", () => {
    const before = parseWorkspaceLayout(DEFAULT_LAYOUT);
    const after = togglePanel(before, "trading", "datasets");
    expect(before.panels.trading).not.toContain("datasets");
    expect(after.panels.trading).toContain("datasets");
    expect(togglePanel(after, "trading", "datasets").panels.trading).not.toContain("datasets");
  });

  it("moves a panel only within its workspace and respects boundaries", () => {
    const before = parseWorkspaceLayout(DEFAULT_LAYOUT);
    const after = movePanel(before, "trading", "charts", -1);
    expect(after.panels.trading.slice(0, 2)).toEqual(["charts", "watchlist"]);
    expect(movePanel(after, "trading", "charts", -1)).toBe(after);
    expect(before.panels.trading.slice(0, 2)).toEqual(["watchlist", "charts"]);
  });

  it("activates only the selected workspace", () => {
    expect(activateWorkspace(parseWorkspaceLayout(DEFAULT_LAYOUT), "risk").activeWorkspace).toBe("risk");
  });
});
