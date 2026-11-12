export const WORKSPACES = [
  { id: "trading", label: "Trading Studio", description: "Charts, order flow, watchlists and execution monitoring." },
  { id: "ai", label: "AI Assistant", description: "Provider-aware research and governed agent workflows." },
  { id: "quant", label: "Quant Lab", description: "Backtests, experiments, regimes and model evaluation." },
  { id: "developer", label: "Developer Studio", description: "Strategy source, diagnostics, tests and terminal tools." },
  { id: "data", label: "Data Center", description: "Market feeds, historical datasets and data quality." },
  { id: "risk", label: "Risk Center", description: "Portfolio exposure, drawdown, limits and audit evidence." },
] as const;

export type WorkspaceId = (typeof WORKSPACES)[number]["id"];

export const PANELS = [
  { id: "watchlist", label: "Market watchlist", group: "Trading" },
  { id: "charts", label: "Multi-chart", group: "Trading" },
  { id: "orderflow", label: "Order flow & footprint", group: "Trading" },
  { id: "execution", label: "Execution monitor", group: "Trading" },
  { id: "copilot", label: "AI copilot", group: "Intelligence" },
  { id: "conversations", label: "Conversations", group: "Intelligence" },
  { id: "experiments", label: "Experiment tracker", group: "Research" },
  { id: "regimes", label: "Market regimes", group: "Research" },
  { id: "strategy", label: "Strategy editor", group: "Engineering" },
  { id: "terminal", label: "Task terminal", group: "Engineering" },
  { id: "datasets", label: "Dataset explorer", group: "Data" },
  { id: "dataquality", label: "Data quality", group: "Data" },
  { id: "exposure", label: "Portfolio exposure", group: "Risk" },
  { id: "audit", label: "Audit timeline", group: "Risk" },
] as const;

export type PanelId = (typeof PANELS)[number]["id"];
export interface PanelGeometry {
  width: 1 | 2;
  height: 1 | 2;
}

export interface WorkspaceLayout {
  version: 2;
  activeWorkspace: WorkspaceId;
  panels: Record<WorkspaceId, PanelId[]>;
  panelGeometry: Record<WorkspaceId, Partial<Record<PanelId, PanelGeometry>>>;
}

const panelIds = new Set<string>(PANELS.map((panel) => panel.id));
const workspaceIds = new Set<string>(WORKSPACES.map((workspace) => workspace.id));

export const DEFAULT_LAYOUT: WorkspaceLayout = {
  version: 2,
  activeWorkspace: "trading",
  panels: {
    trading: ["watchlist", "charts", "orderflow", "execution", "copilot"],
    ai: ["copilot", "conversations", "datasets"],
    quant: ["experiments", "regimes", "datasets", "copilot"],
    developer: ["strategy", "terminal", "audit"],
    data: ["datasets", "dataquality", "watchlist"],
    risk: ["exposure", "audit", "execution"],
  },
  panelGeometry: {
    trading: {},
    ai: {},
    quant: {},
    developer: {},
    data: {},
    risk: {},
  },
};

export function parseWorkspaceLayout(input: unknown): WorkspaceLayout {
  if (!input || typeof input !== "object" || Array.isArray(input)) return cloneDefault();
  const value = input as Record<string, unknown>;
  if ((value.version !== 1 && value.version !== 2) || !workspaceIds.has(String(value.activeWorkspace))) return cloneDefault();
  const rawPanels = value.panels;
  if (!rawPanels || typeof rawPanels !== "object" || Array.isArray(rawPanels)) return cloneDefault();
  const panels = {} as Record<WorkspaceId, PanelId[]>;
  const panelGeometry = {} as Record<WorkspaceId, Partial<Record<PanelId, PanelGeometry>>>;
  const rawGeometry = value.panelGeometry && typeof value.panelGeometry === "object" && !Array.isArray(value.panelGeometry)
    ? value.panelGeometry as Record<string, unknown>
    : {};
  for (const workspace of WORKSPACES) {
    const raw = (rawPanels as Record<string, unknown>)[workspace.id];
    const seen = new Set<string>();
    const safe: PanelId[] = [];
    if (Array.isArray(raw)) {
      for (const candidate of raw.slice(0, 24)) {
        if (typeof candidate === "string" && panelIds.has(candidate) && !seen.has(candidate)) {
          seen.add(candidate);
          safe.push(candidate as PanelId);
        }
      }
    }
    panels[workspace.id] = safe;
    const sizes: Partial<Record<PanelId, PanelGeometry>> = {};
    const rawSizes = rawGeometry[workspace.id];
    if (rawSizes && typeof rawSizes === "object" && !Array.isArray(rawSizes)) {
      for (const [candidate, dimensions] of Object.entries(rawSizes)) {
        if (!safe.includes(candidate as PanelId) || !dimensions || typeof dimensions !== "object" || Array.isArray(dimensions)) continue;
        const item = dimensions as Record<string, unknown>;
        if ((item.width === 1 || item.width === 2) && (item.height === 1 || item.height === 2)) {
          sizes[candidate as PanelId] = { width: item.width, height: item.height };
        }
      }
    }
    panelGeometry[workspace.id] = sizes;
  }
  return { version: 2, activeWorkspace: value.activeWorkspace as WorkspaceId, panels, panelGeometry };
}

function cloneDefault(): WorkspaceLayout {
  return {
    version: 2,
    activeWorkspace: DEFAULT_LAYOUT.activeWorkspace,
    panels: Object.fromEntries(
      WORKSPACES.map(({ id }) => [id, [...DEFAULT_LAYOUT.panels[id]]]),
    ) as Record<WorkspaceId, PanelId[]>,
    panelGeometry: Object.fromEntries(
      WORKSPACES.map(({ id }) => [id, { ...DEFAULT_LAYOUT.panelGeometry[id] }]),
    ) as Record<WorkspaceId, Partial<Record<PanelId, PanelGeometry>>>,
  };
}

export function togglePanel(layout: WorkspaceLayout, workspace: WorkspaceId, panel: PanelId): WorkspaceLayout {
  const current = layout.panels[workspace];
  const removing = current.includes(panel);
  const panels = removing
    ? current.filter((item) => item !== panel)
    : current.length < 24 ? [...current, panel] : current;
  const panelGeometry = { ...layout.panelGeometry[workspace] };
  if (removing) delete panelGeometry[panel];
  return {
    ...layout,
    panels: { ...layout.panels, [workspace]: panels },
    panelGeometry: { ...layout.panelGeometry, [workspace]: panelGeometry },
  };
}

export function resizePanel(
  layout: WorkspaceLayout,
  workspace: WorkspaceId,
  panel: PanelId,
  dimension: "width" | "height",
  value: 1 | 2,
): WorkspaceLayout {
  if (!layout.panels[workspace].includes(panel)) return layout;
  const current = layout.panelGeometry[workspace][panel] ?? { width: 1, height: 1 };
  if (current[dimension] === value) return layout;
  return {
    ...layout,
    panelGeometry: {
      ...layout.panelGeometry,
      [workspace]: {
        ...layout.panelGeometry[workspace],
        [panel]: { ...current, [dimension]: value },
      },
    },
  };
}

export function reorderPanel(
  layout: WorkspaceLayout,
  workspace: WorkspaceId,
  source: PanelId,
  target: PanelId,
): WorkspaceLayout {
  const current = [...layout.panels[workspace]];
  const from = current.indexOf(source);
  const to = current.indexOf(target);
  if (from < 0 || to < 0 || from === to) return layout;
  current.splice(from, 1);
  current.splice(to, 0, source);
  return { ...layout, panels: { ...layout.panels, [workspace]: current } };
}

export function movePanel(layout: WorkspaceLayout, workspace: WorkspaceId, panel: PanelId, direction: -1 | 1): WorkspaceLayout {
  const current = [...layout.panels[workspace]];
  const index = current.indexOf(panel);
  const target = index + direction;
  if (index < 0 || target < 0 || target >= current.length) return layout;
  [current[index], current[target]] = [current[target], current[index]];
  return { ...layout, panels: { ...layout.panels, [workspace]: current } };
}

export function activateWorkspace(layout: WorkspaceLayout, workspace: WorkspaceId): WorkspaceLayout {
  return { ...layout, activeWorkspace: workspace };
}
