import { describe, expect, it } from "vitest";
import {
  activateWorkspace,
  DEFAULT_LAYOUT,
  movePanel,
  parseWorkspaceLayout,
  reorderPanel,
  resizePanel,
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

  it("migrates legacy layouts and validates per-panel dock geometry", () => {
    const legacy = parseWorkspaceLayout({
      version: 1,
      activeWorkspace: "trading",
      panels: DEFAULT_LAYOUT.panels,
    });
    expect(legacy.version).toBe(2);
    expect(legacy.panelGeometry.trading).toEqual({});

    const resized = parseWorkspaceLayout({
      ...DEFAULT_LAYOUT,
      panelGeometry: {
        ...DEFAULT_LAYOUT.panelGeometry,
        trading: {
          charts: { width: 2, height: 1 },
          watchlist: { width: 1, height: 2 },
          unknown: { width: 2, height: 2 },
          orderflow: { width: 3, height: 1 },
        },
      },
    });
    expect(resized.panelGeometry.trading).toEqual({
      charts: { width: 2, height: 1 },
      watchlist: { width: 1, height: 2 },
    });
  });

  it("resizes and reorders only docked panels without mutating prior state", () => {
    const before = parseWorkspaceLayout(DEFAULT_LAYOUT);
    const resized = resizePanel(before, "trading", "charts", "width", 2);
    expect(resized.panelGeometry.trading.charts).toEqual({ width: 2, height: 1 });
    expect(before.panelGeometry.trading.charts).toBeUndefined();
    const reordered = reorderPanel(resized, "trading", "copilot", "watchlist");
    expect(reordered.panels.trading[0]).toBe("copilot");
    expect(resized.panels.trading[0]).toBe("watchlist");
    expect(reorderPanel(reordered, "trading", "copilot", "copilot")).toBe(reordered);
  });

  it("removes saved geometry when a panel is undocked", () => {
    const resized = resizePanel(parseWorkspaceLayout(DEFAULT_LAYOUT), "trading", "charts", "height", 2);
    const undocked = togglePanel(resized, "trading", "charts");
    expect(undocked.panels.trading).not.toContain("charts");
    expect(undocked.panelGeometry.trading.charts).toBeUndefined();
  });

  it("activates only the selected workspace", () => {
    expect(activateWorkspace(parseWorkspaceLayout(DEFAULT_LAYOUT), "risk").activeWorkspace).toBe("risk");
  });
});
