import { afterEach, describe, expect, it, vi } from "vitest";
import { DEFAULT_LAYOUT } from "./workspaceModel";
import { loadWorkspaceLayout, saveWorkspaceLayout } from "./workspaceStore";

const ETAG = '"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"';
afterEach(() => vi.unstubAllGlobals());

describe("OS-backed workspace preferences client", () => {
  it("loads layout plus revision from the authenticated local gateway", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: new Headers({ ETag: ETAG }),
      json: async () => DEFAULT_LAYOUT,
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(loadWorkspaceLayout()).resolves.toEqual({ layout: DEFAULT_LAYOUT, etag: ETAG });
    expect(fetchMock).toHaveBeenCalledWith("/operator/v1/workspace-layout",
      expect.objectContaining({ credentials: "same-origin", cache: "no-store" }));
  });

  it("uses compare-and-swap and notifies other windows after a save", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, status: 200, headers: new Headers({ ETag: ETAG }),
      json: async () => DEFAULT_LAYOUT,
    });
    const channel = { postMessage: vi.fn(), close: vi.fn() };
    class MockBroadcastChannel {
      postMessage = channel.postMessage;
      close = channel.close;
      constructor(_name: string) {}
    }
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("BroadcastChannel", MockBroadcastChannel);
    await expect(saveWorkspaceLayout(DEFAULT_LAYOUT, ETAG)).resolves.toEqual({ layout: DEFAULT_LAYOUT, etag: ETAG });
    expect(fetchMock).toHaveBeenCalledWith("/operator/v1/workspace-layout",
      expect.objectContaining({ method: "PUT",
        headers: { "Content-Type": "application/json", "If-Match": ETAG },
        body: JSON.stringify(DEFAULT_LAYOUT), credentials: "same-origin", cache: "no-store" }));
    expect(channel.postMessage).toHaveBeenCalledWith({ type: "layout-updated" });
    expect(channel.close).toHaveBeenCalledOnce();
  });

  it("surfaces stale-window conflicts", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 409 }));
    await expect(saveWorkspaceLayout(DEFAULT_LAYOUT, ETAG)).rejects.toThrow("workspace_layout_conflict");
  });

  it("does not claim persistence when the gateway rejects a write", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 503 }));
    await expect(saveWorkspaceLayout(DEFAULT_LAYOUT, ETAG)).rejects.toThrow("workspace_layout_save_failed_503");
  });
});
