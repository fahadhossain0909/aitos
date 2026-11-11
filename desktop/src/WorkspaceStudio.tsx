import { useEffect, useMemo, useRef, useState } from "react";
import {
  activateWorkspace,
  DEFAULT_LAYOUT,
  movePanel,
  PANELS,
  parseWorkspaceLayout,
  togglePanel,
  WORKSPACES,
  type WorkspaceId,
  type WorkspaceLayout,
  type PanelId,
} from "./workspaceModel";

const STORAGE_KEY = "aitos.workspace-layout.v1";

function readLayout(): WorkspaceLayout {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    return stored ? parseWorkspaceLayout(JSON.parse(stored)) : parseWorkspaceLayout(DEFAULT_LAYOUT);
  } catch {
    return parseWorkspaceLayout(DEFAULT_LAYOUT);
  }
}

export function WorkspaceStudio() {
  const [layout, setLayout] = useState<WorkspaceLayout>(() => readLayout());
  const [query, setQuery] = useState("");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [storageAvailable, setStorageAvailable] = useState(true);
  const searchRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(layout));
      setStorageAvailable(true);
    } catch {
      setStorageAvailable(false);
    }
  }, [layout]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen(true);
      } else if (event.key === "Escape") {
        setPaletteOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (paletteOpen) searchRef.current?.focus();
  }, [paletteOpen]);

  const active = WORKSPACES.find((workspace) => workspace.id === layout.activeWorkspace)!;
  const activePanels = layout.panels[layout.activeWorkspace];
  const filteredWorkspaces = useMemo(
    () => WORKSPACES.filter((workspace) => `${workspace.label} ${workspace.description}`.toLowerCase().includes(query.toLowerCase())),
    [query],
  );
  const filteredPanels = useMemo(
    () => PANELS.filter((panel) => `${panel.label} ${panel.group}`.toLowerCase().includes(query.toLowerCase())),
    [query],
  );

  const update = (next: WorkspaceLayout) => setLayout(next);
  const activate = (id: WorkspaceId) => {
    update(activateWorkspace(layout, id));
    setPaletteOpen(false);
    setQuery("");
  };

  return (
    <section className="workspace-studio" aria-label="Workspace Studio">
      <div className="workspace-studio-toolbar">
        <div>
          <span className="small-cap">PERSONAL WORKSPACE CONTROL</span>
          <h2>One application. Six focused environments.</h2>
          <p>Choose a workspace, arrange its panels and keep the layout on this device.</p>
        </div>
        <button className="button secondary" onClick={() => setPaletteOpen(true)} aria-keyshortcuts="Control+K Meta+K">
          <span>⌘</span> Command palette <kbd>Ctrl K</kbd>
        </button>
      </div>

      <div className="workspace-studio-grid">
        <aside className="workspace-picker" aria-label="Available workspaces">
          <div className="workspace-picker-heading">WORKSPACES <span>{WORKSPACES.length}</span></div>
          {WORKSPACES.map((workspace, index) => (
            <button
              key={workspace.id}
              className={layout.activeWorkspace === workspace.id ? "workspace-choice active" : "workspace-choice"}
              onClick={() => activate(workspace.id)}
              aria-pressed={layout.activeWorkspace === workspace.id}
            >
              <span className="workspace-choice-index">{String(index + 1).padStart(2, "0")}</span>
              <span><strong>{workspace.label}</strong><small>{workspace.description}</small></span>
            </button>
          ))}
        </aside>

        <div className="workspace-editor">
          <div className="workspace-editor-heading">
            <div><span className="small-cap">ACTIVE ENVIRONMENT</span><h3>{active.label}</h3><p>{active.description}</p></div>
            <span className="workspace-local-badge">{storageAvailable ? "SAVED ON DEVICE" : "LOCAL STORAGE UNAVAILABLE"}</span>
          </div>
          <div className="workspace-panel-heading">
            <strong>Docked panels</strong><span>{activePanels.length} active · {PANELS.length} available</span>
          </div>
          <div className="workspace-panel-list">
            {activePanels.length ? activePanels.map((panelId, index) => {
              const panel = PANELS.find((item) => item.id === panelId)!;
              return (
                <div className="workspace-panel-row" key={panelId}>
                  <span className="workspace-panel-grip" aria-hidden="true">⠿</span>
                  <span className="workspace-panel-order">{String(index + 1).padStart(2, "0")}</span>
                  <span className="workspace-panel-name"><strong>{panel.label}</strong><small>{panel.group}</small></span>
                  <button className="icon-button" onClick={() => update(movePanel(layout, layout.activeWorkspace, panelId, -1))} disabled={index === 0} aria-label={`Move ${panel.label} up`}>↑</button>
                  <button className="icon-button" onClick={() => update(movePanel(layout, layout.activeWorkspace, panelId, 1))} disabled={index === activePanels.length - 1} aria-label={`Move ${panel.label} down`}>↓</button>
                  <button className="icon-button" onClick={() => update(togglePanel(layout, layout.activeWorkspace, panelId))} aria-label={`Remove ${panel.label}`}>×</button>
                </div>
              );
            }) : <div className="workspace-empty">No panels are docked. Add a panel from the library.</div>}
          </div>
          <div className="workspace-library-heading"><strong>Panel library</strong><span>Enable the panels you need in this workspace</span></div>
          <div className="workspace-library">
            {PANELS.filter((panel) => !activePanels.includes(panel.id)).map((panel) => (
              <button key={panel.id} className="workspace-library-item" onClick={() => update(togglePanel(layout, layout.activeWorkspace, panel.id))}>
                <span>＋</span><strong>{panel.label}</strong><small>{panel.group}</small>
              </button>
            ))}
          </div>
          <div className="workspace-studio-footnote">
            <span>LAYOUT STATE</span>
            <span>Version 1 · Local device profile · No runtime or trading settings changed</span>
          </div>
        </div>
      </div>

      {paletteOpen && (
        <div className="workspace-palette-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && setPaletteOpen(false)}>
          <section className="workspace-palette" role="dialog" aria-modal="true" aria-label="Command palette">
            <input ref={searchRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search workspaces and panels…" aria-label="Search commands" />
            <div className="workspace-palette-results">
              <span className="small-cap">SWITCH WORKSPACE</span>
              {filteredWorkspaces.map((workspace) => (
                <button key={workspace.id} onClick={() => activate(workspace.id)}><strong>{workspace.label}</strong><small>{workspace.description}</small><kbd>↵</kbd></button>
              ))}
              <span className="small-cap">PANEL LIBRARY</span>
              {filteredPanels.slice(0, 8).map((panel) => (
                <button key={panel.id} onClick={() => { update(togglePanel(layout, layout.activeWorkspace, panel.id)); setPaletteOpen(false); setQuery(""); }}>
                  <strong>{panel.label}</strong><small>{panel.group} · {active.label}</small><kbd>＋</kbd>
                </button>
              ))}
              {!filteredWorkspaces.length && !filteredPanels.length && <p className="workspace-empty">No matching workspace or panel.</p>}
            </div>
            <div className="workspace-palette-footer"><span>↑↓ Navigate by searching</span><span>ESC Close</span></div>
          </section>
        </div>
      )}
    </section>
  );
}
