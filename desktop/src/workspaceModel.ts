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
export interface WorkspaceLayout {
  version: 1;
  activeWorkspace: WorkspaceId;
  panels: Record<WorkspaceId, PanelId[]>;
}

const panelIds = new Set<string>(PANELS.map((panel) => panel.id));
const workspaceIds = new Set<string>(WORKSPACES.map((workspace) => workspace.id));

export const DEFAULT_LAYOUT: WorkspaceLayout = {
  version: 1,
  activeWorkspace: "trading",
  panels: {
    trading: ["watchlist", "charts", "orderflow", "execution", "copilot"],
    ai: ["copilot", "conversations", "datasets"],
    quant: ["experiments", "regimes", "datasets", "copilot"],
    developer: ["strategy", "terminal", "audit"],
    data: ["datasets", "dataquality", "watchlist"],
    risk: ["exposure", "audit", "execution"],
  },
};

export function parseWorkspaceLayout(input: unknown): WorkspaceLayout {
  if (!input || typeof input !== "object" || Array.isArray(input)) return cloneDefault();
  const value = input as Record<string, unknown>;
  if (value.version !== 1 || !workspaceIds.has(String(value.activeWorkspace))) return cloneDefault();
  const rawPanels = value.panels;
  if (!rawPanels || typeof rawPanels !== "object" || Array.isArray(rawPanels)) return cloneDefault();
  const panels = {} as Record<WorkspaceId, PanelId[]>;
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
  }
  return { version: 1, activeWorkspace: value.activeWorkspace as WorkspaceId, panels };
}

function cloneDefault(): WorkspaceLayout {
  return {
    version: 1,
    activeWorkspace: DEFAULT_LAYOUT.activeWorkspace,
    panels: Object.fromEntries(
      WORKSPACES.map(({ id }) => [id, [...DEFAULT_LAYOUT.panels[id]]]),
    ) as Record<WorkspaceId, PanelId[]>,
  };
}

export function togglePanel(layout: WorkspaceLayout, workspace: WorkspaceId, panel: PanelId): WorkspaceLayout {
  const current = layout.panels[workspace];
  const panels = current.includes(panel)
    ? current.filter((item) => item !== panel)
    : current.length < 24 ? [...current, panel] : current;
  return { ...layout, panels: { ...layout.panels, [workspace]: panels } };
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
