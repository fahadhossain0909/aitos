import { consoleFetch } from "./operatorSession";
import { parseWorkspaceLayout, type WorkspaceLayout } from "./workspaceModel";

const WORKSPACE_LAYOUT_ENDPOINT = "/operator/v1/workspace-layout";

export async function loadWorkspaceLayout(): Promise<WorkspaceLayout> {
  const response = await consoleFetch(WORKSPACE_LAYOUT_ENDPOINT);
  if (!response.ok) {
    throw new Error(`workspace_layout_load_failed_${response.status}`);
  }
  return parseWorkspaceLayout(await response.json());
}

export async function saveWorkspaceLayout(
  layout: WorkspaceLayout,
): Promise<WorkspaceLayout> {
  const response = await consoleFetch(WORKSPACE_LAYOUT_ENDPOINT, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(layout),
  });
  if (!response.ok) {
    throw new Error(`workspace_layout_save_failed_${response.status}`);
  }
  return parseWorkspaceLayout(await response.json());
}
