import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_LAYOUT } from "./workspaceModel";
import { loadWorkspaceLayout, saveWorkspaceLayout } from "./workspaceStore";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("OS-backed workspace preferences client", () => {
  it("loads the saved layout from the authenticated local gateway", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => DEFAULT_LAYOUT,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(loadWorkspaceLayout()).resolves.toEqual(DEFAULT_LAYOUT);
    expect(fetchMock).toHaveBeenCalledWith(
      "/operator/v1/workspace-layout",
      expect.objectContaining({
        credentials: "same-origin",
        cache: "no-store",
      }),
    );
  });

  it("persists layout changes through a same-origin PUT", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => DEFAULT_LAYOUT,
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(saveWorkspaceLayout(DEFAULT_LAYOUT)).resolves.toEqual(DEFAULT_LAYOUT);
    expect(fetchMock).toHaveBeenCalledWith(
      "/operator/v1/workspace-layout",
      expect.objectContaining({
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(DEFAULT_LAYOUT),
        credentials: "same-origin",
        cache: "no-store",
      }),
    );
  });

  it("does not silently claim persistence when the gateway rejects a write", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: false, status: 503 }),
    );

    await expect(saveWorkspaceLayout(DEFAULT_LAYOUT)).rejects.toThrow(
      "workspace_layout_save_failed_503",
    );
  });
});
