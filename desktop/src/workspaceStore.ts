import { consoleFetch } from "./operatorSession";
import { parseWorkspaceLayout, type WorkspaceLayout } from "./workspaceModel";

const ENDPOINT = "/operator/v1/workspace-layout";
const CHANNEL = "aitos-workspace-layout-v1";

export interface WorkspaceSnapshot {
  layout: WorkspaceLayout;
  etag: string;
}

function parseSnapshot(response: Response, body: unknown): WorkspaceSnapshot {
  const etag = response.headers.get("ETag") ?? "";
  if (!/^"[0-9a-f]{64}"$/.test(etag)) throw new Error("workspace_layout_revision_invalid");
  return { layout: parseWorkspaceLayout(body), etag };
}

export async function loadWorkspaceLayout(): Promise<WorkspaceSnapshot> {
  const response = await consoleFetch(ENDPOINT);
  if (!response.ok) throw new Error(`workspace_layout_load_failed_${response.status}`);
  return parseSnapshot(response, await response.json());
}

export async function saveWorkspaceLayout(layout: WorkspaceLayout, etag: string): Promise<WorkspaceSnapshot> {
  if (!/^"[0-9a-f]{64}"$/.test(etag)) throw new Error("workspace_layout_revision_required");
  const response = await consoleFetch(ENDPOINT, {
    method: "PUT",
    headers: { "Content-Type": "application/json", "If-Match": etag },
    body: JSON.stringify(layout),
  });
  if (response.status === 409) throw new Error("workspace_layout_conflict");
  if (!response.ok) throw new Error(`workspace_layout_save_failed_${response.status}`);
  const saved = parseSnapshot(response, await response.json());
  if (typeof BroadcastChannel !== "undefined") {
    const channel = new BroadcastChannel(CHANNEL);
    channel.postMessage({ type: "layout-updated" });
    channel.close();
  }
  return saved;
}

export function subscribeWorkspaceLayout(onUpdate: (snapshot: WorkspaceSnapshot) => void): () => void {
  if (typeof BroadcastChannel === "undefined") return () => undefined;
  const channel = new BroadcastChannel(CHANNEL);
  channel.onmessage = (event: MessageEvent<unknown>) => {
    if (event.data && typeof event.data === "object" &&
        (event.data as { type?: unknown }).type === "layout-updated") {
      void loadWorkspaceLayout().then(onUpdate).catch(() => undefined);
    }
  };
  return () => channel.close();
}
