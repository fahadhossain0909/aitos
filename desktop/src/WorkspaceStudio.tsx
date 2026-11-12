import { useEffect, useMemo, useRef, useState } from "react";
import { loadWorkspaceLayout, saveWorkspaceLayout, subscribeWorkspaceLayout } from "./workspaceStore";
import {
  activateWorkspace,
  DEFAULT_LAYOUT,
  movePanel,
  PANELS,
  parseWorkspaceLayout,
  reorderPanel,
  resizePanel,
  togglePanel,
  WORKSPACES,
  type WorkspaceId,
  type WorkspaceLayout,
  type PanelId,
} from "./workspaceModel";

export function WorkspaceStudio({
  onOpenPanel,
}: {
  onOpenPanel: (panel: PanelId) => void;
}) {
  const [layout, setLayout] = useState<WorkspaceLayout>(() => parseWorkspaceLayout(DEFAULT_LAYOUT));
  const [layoutLoaded, setLayoutLoaded] = useState(false);
  const [query, setQuery] = useState("");
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [storageAvailable, setStorageAvailable] = useState(true);
  const saveQueue = useRef<Promise<void>>(Promise.resolve());
  const etag = useRef("");
  const skipNextSave = useRef(false);
  const layoutRef = useRef(layout);
  layoutRef.current = layout;
  const searchRef = useRef<HTMLInputElement>(null);
  const draggedPanel = useRef<PanelId | null>(null);

  useEffect(() => {
    let active = true;
    loadWorkspaceLayout()
      .then((saved) => {
        if (active) {
          etag.current = saved.etag;
          skipNextSave.current = true;
          layoutRef.current = saved.layout;
          setLayout(saved.layout);
          setStorageAvailable(true);
        }
      })
      .catch(() => {
        if (active) setStorageAvailable(false);
      })
      .finally(() => {
        if (active) setLayoutLoaded(true);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!layoutLoaded || !storageAvailable) return;
    if (skipNextSave.current) {
      skipNextSave.current = false;
      return;
    }
    saveQueue.current = saveQueue.current
      .catch(() => undefined)
      .then(async () => {
        if (layoutRef.current !== layout) return;
        const saved = await saveWorkspaceLayout(layout, etag.current);
        etag.current = saved.etag;
      })
      .catch(async (error: unknown) => {
        if (error instanceof Error && error.message === "workspace_layout_conflict") {
          try {
            const latest = await loadWorkspaceLayout();
            etag.current = latest.etag;
            skipNextSave.current = true;
            layoutRef.current = latest.layout;
            setLayout(latest.layout);
            return;
          } catch {
            // Fall through to the unavailable state.
          }
        }
        setStorageAvailable(false);
      });
  }, [layout, layoutLoaded, storageAvailable]);

  useEffect(() => subscribeWorkspaceLayout((remote) => {
    etag.current = remote.etag;
    skipNextSave.current = true;
    layoutRef.current = remote.layout;
    setLayout(remote.layout);
  }), []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen(true);
      } else if (event.key === "Escape") {
        setPaletteOpen(false);
      }
    };
    const openPalette = () => setPaletteOpen(true);
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("aitos:open-command-palette", openPalette);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("aitos:open-command-palette", openPalette);
    };
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

  if (!layoutLoaded) {
    return (
      <section className="workspace-studio" aria-label="Workspace Studio" aria-busy="true">
        <div className="workspace-empty">Loading saved workspace layout…</div>
      </section>
    );
  }

  return (
    <section className="workspace-studio" aria-label="Workspace Studio">
      <div className="workspace-studio-toolbar">
        <div>
          <span className="small-cap">PERSONAL WORKSPACE CONTROL</span>
          <h2>One application. Six focused environments.</h2>
          <p>Choose a workspace, resize and reorder its panels, and persist the layout in your OS configuration folder.</p>
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
            <span className="workspace-local-badge">{storageAvailable ? "SAVED LOCALLY" : "LOCAL SETTINGS UNAVAILABLE"}</span>
          </div>
          <div className="workspace-panel-heading">
            <strong>Resizable docked panels</strong><span>{activePanels.length} active · drag to reorder</span>
          </div>
          <div className="workspace-panel-grid" aria-label="Resizable docked panels">
            {activePanels.length ? activePanels.map((panelId, index) => {
              const panel = PANELS.find((item) => item.id === panelId)!;
              const geometry = layout.panelGeometry[layout.activeWorkspace][panelId] ?? { width: 1, height: 1 };
              return (
                <article
                  className={`workspace-panel-row ${geometry.width === 2 ? "panel-wide" : ""} ${geometry.height === 2 ? "panel-tall" : ""}`}
                  key={panelId}
                  draggable
                  onDragStart={(event) => {
                    draggedPanel.current = panelId;
                    event.dataTransfer.effectAllowed = "move";
                    event.dataTransfer.setData("text/plain", panelId);
                  }}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={(event) => {
                    event.preventDefault();
                    const source = draggedPanel.current;
                    if (source) update(reorderPanel(layout, layout.activeWorkspace, source, panelId));
                    draggedPanel.current = null;
                  }}
                  onDragEnd={() => { draggedPanel.current = null; }}
                >
                  <span className="workspace-panel-grip" aria-hidden="true">⠿</span>
                  <span className="workspace-panel-order">{String(index + 1).padStart(2, "0")}</span>
                  <span className="workspace-panel-name">
                    <strong>{panel.label}</strong>
                    <small>{panel.group} · {geometry.width === 2 ? "Wide" : "Standard"} · {geometry.height === 2 ? "Tall" : "Compact"}</small>
                  </span>
                  <div className="workspace-panel-actions">
                    <button className="icon-button" onClick={() => onOpenPanel(panelId)} aria-label={`Open ${panel.label}`} title="Open existing operator page">↗</button>
                    <button className="icon-button" onClick={() => update(resizePanel(layout, layout.activeWorkspace, panelId, "width", geometry.width === 2 ? 1 : 2))} aria-label={geometry.width === 2 ? `Narrow ${panel.label}` : `Widen ${panel.label}`} title={geometry.width === 2 ? "Narrow panel" : "Widen panel"}>↔</button>
                    <button className="icon-button" onClick={() => update(resizePanel(layout, layout.activeWorkspace, panelId, "height", geometry.height === 2 ? 1 : 2))} aria-label={geometry.height === 2 ? `Shorten ${panel.label}` : `Expand ${panel.label}`} title={geometry.height === 2 ? "Shorten panel" : "Expand panel"}>↕</button>
                    <button className="icon-button" onClick={() => update(movePanel(layout, layout.activeWorkspace, panelId, -1))} disabled={index === 0} aria-label={`Move ${panel.label} up`}>↑</button>
                    <button className="icon-button" onClick={() => update(movePanel(layout, layout.activeWorkspace, panelId, 1))} disabled={index === activePanels.length - 1} aria-label={`Move ${panel.label} down`}>↓</button>
                    <button className="icon-button" onClick={() => update(togglePanel(layout, layout.activeWorkspace, panelId))} aria-label={`Undock ${panel.label}`}>×</button>
                  </div>
                </article>
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
            <span>Version 1 · OS configuration storage · No runtime or trading settings changed</span>
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
