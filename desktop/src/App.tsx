import ProtectionPanel from "./ProtectionPanel";
import AccountPanel from "./AccountPanel";
import { parseRuntimeCapabilities, type RuntimeCapabilities } from "./runtimeCapabilities";
import { consoleFetch } from "./operatorSession";
import ResearchPlanner from "./ResearchPlanner";
import PersistencePanel from "./PersistencePanel";
import PerformancePanel from "./PerformancePanel";
import { parseSnapshot, parseHistory } from "./consoleClient";
import ReadinessPanel from "./ReadinessPanel";
import { useState, useEffect, useRef } from "react";
import {
  Activity,
  ArrowDownLeft,
  ArrowDownToLine,
  ArrowRight,
  ArrowUpRight,
  Bell,
  BookOpen,
  Box,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  Command,
  Database,
  FlaskConical,
  Globe,
  Layers3,
  LayoutDashboard,
  LockKeyhole,
  Maximize2,
  MoreHorizontal,
  Pause,
  Play,
  Plus,
  Radio,
  Search,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Terminal,
  TrendingUp,
  Wallet,
  X,
  Zap,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import {
  samplePositions,
  sampleHistory,
  sampleStages,
  markets,
  money,
  number,
  exportCsv,
  download,
  checkDraft,
} from "./data";
import type { Position, Snapshot, Stage } from "./data";

type Page =
  | "overview"
  | "positions"
  | "history"
  | "research"
  | "telemetry"
  | "exchanges"
  | "settings"
  | "readiness"
  | "performance"
  | "persistence";
type Modal = "launch" | "live" | "connection" | "notifications" | "help" | null;
const navigation: { id: Page; label: string; icon: LucideIcon }[] = [
  { id: "overview", label: "Overview", icon: LayoutDashboard },
  { id: "positions", label: "Portfolio", icon: Wallet },
  { id: "history", label: "Trade journal", icon: BookOpen },
  { id: "performance", label: "Performance", icon: TrendingUp },
  { id: "research", label: "Research lab", icon: FlaskConical },
  { id: "telemetry", label: "Observability", icon: Activity },
  { id: "exchanges", label: "Connections", icon: Globe },
  { id: "settings", label: "Configuration", icon: SlidersHorizontal },
  { id: "readiness", label: "Quality & readiness", icon: ShieldCheck },
  { id: "persistence", label: "Persistence & recovery", icon: Database },
];
function Icon({
  as: Component,
  ...props
}: {
  as: LucideIcon;
  size?: number;
  className?: string;
}) {
  return <Component size={16} strokeWidth={1.7} {...props} />;
}
function Pill({
  children,
  tone = "muted",
}: {
  children: React.ReactNode;
  tone?: string;
}) {
  return <span className={`pill ${tone}`}>{children}</span>;
}
function Empty({ title, description }: { title: string; description: string }) {
  return (
    <div className="empty">
      <Database size={27} />
      <h3>{title}</h3>
      <p>{description}</p>
    </div>
  );
}
function Sparkline({
  positive = true,
  seed = 0,
}: {
  positive?: boolean;
  seed?: number;
}) {
  const path = Array.from(
    { length: 24 },
    (_, i) =>
      `${i ? "L" : "M"}${i * 3.5},${20 - (Math.sin(i * 0.7 + seed) * 4 + i * 0.45) * (positive ? 1 : -0.6)}`,
  ).join(" ");
  return (
    <svg
      viewBox="0 0 84 34"
      className={`sparkline ${positive ? "positive" : "negative"}`}
      aria-hidden="true"
    >
      <path d={path} fill="none" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  );
}
function Coin({ symbol }: { symbol: string }) {
  const base = symbol.replace("USDT", "");
  return (
    <span className={`coin coin-${base.toLowerCase()}`}>
      {base === "BTC"
        ? "₿"
        : base === "ETH"
          ? "Ξ"
          : base === "SOL"
            ? "≋"
            : base[0]}
    </span>
  );
}
function PositionTable({
  rows,
  compact = false,
  onSelect,
}: {
  rows: Position[];
  compact?: boolean;
  onSelect: (p: Position) => void;
}) {
  return rows.length ? (
    <div className="table-scroll">
      <table>
        <thead>
          <tr>
            <th>Instrument</th>
            <th>Direction</th>
            <th>Entry price</th>
            {!compact && <th>Quantity</th>}
            <th>Position value</th>
            <th>Recorded P&L</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr
              key={p.trade_id}
              onClick={() => onSelect(p)}
              tabIndex={0}
              onKeyDown={(e) => e.key === "Enter" && onSelect(p)}
              aria-label={`Inspect ${p.symbol} position`}
            >
              <td>
                <div className="instrument">
                  <Coin symbol={p.symbol} />
                  <div>
                    <strong>
                      {p.symbol.replace("USDT", "")}
                      <span className="dim"> / USDT</span>
                    </strong>
                    <small>Perpetual futures</small>
                  </div>
                </div>
              </td>
              <td>
                <span
                  className={`direction ${p.side === "LONG" ? "positive" : "negative"}`}
                >
                  {p.side === "LONG" ? (
                    <ArrowUpRight size={13} />
                  ) : (
                    <ArrowDownLeft size={13} />
                  )}{" "}
                  {p.side}
                </span>
              </td>
              <td className="mono">{money(p.entry_price)}</td>
              {!compact && <td className="mono">{number(p.quantity, 5)}</td>}
              <td className="mono">{money(p.position_size_usd)}</td>
              <td
                className={`mono ${p.pnl != null && p.pnl < 0 ? "negative" : "positive"}`}
              >
                {p.pnl != null && p.pnl > 0 ? "+" : ""}
                {money(p.pnl)}
              </td>
              <td>
                <ChevronRight size={15} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  ) : (
    <Empty
      title="No positions to display"
      description="Attach a running backend to read actual open positions. No positions are inferred from a disconnected runtime."
    />
  );
}
function EquityChart({
  demo,
  range,
  onRange,
}: {
  demo: boolean;
  range: string;
  onRange: (s: string) => void;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const start = (
    {
      "1D": 127608.25,
      "1W": 124820,
      "1M": 119240,
      "3M": 110310,
      "1Y": 100000,
    } as Record<string, number>
  )[range];
  const end = 128450.25;
  const values = Array.from(
    { length: 85 },
    (_, i) =>
      start +
      (end - start) * (i / 84) +
      Math.sin(i * 0.34) *
        (end - start) *
        0.045 *
        Math.sin((i / 84) * Math.PI) +
      Math.sin(i * 0.94) * (end - start) * 0.018 * Math.sin((i / 84) * Math.PI),
  );
  const width = 880,
    height = 224;
  const pts = values.map((v, i) => [
    (i / 84) * width,
    201 - ((v - start) / (end - start)) * 172,
  ]);
  const line = pts
    .map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`)
    .join(" ");
  const ix = hover ?? 84;
  return (
    <section className="panel equity-panel">
      <div className="panel-heading">
        <div className="panel-title">
          <span className="small-cap">PORTFOLIO VALUE</span>
          <span className="tiny-tag">
            {demo ? "SAMPLE SERIES" : "NO EQUITY FEED"}
          </span>
        </div>
        <div className="segmented">
          {["1D", "1W", "1M", "3M", "1Y"].map((r) => (
            <button
              key={r}
              className={range === r ? "active" : ""}
              onClick={() => {
                onRange(r);
                setHover(null);
              }}
            >
              {r}
            </button>
          ))}
        </div>
      </div>
      <div className="equity-number">
        {demo ? money(values[ix]) : "—"}
        <span className="unit">USD</span>
      </div>
      <div className="equity-context">
        {demo ? (
          <>
            <span className="positive">
              <ArrowUpRight size={14} /> {money(end - start)} (
              {((end / start - 1) * 100).toFixed(2)}%)
            </span>
            <span>this period · illustrative</span>
          </>
        ) : (
          <span>Historical account-equity feed has not been attached.</span>
        )}
      </div>
      <div className="chart-container">
        {demo ? (
          <>
            <div className="chart-y-labels">
              {[130000, 125000, 120000, 115000].map((v, i) => (
                <span key={v}>
                  {number(start + (end - start) * (1.12 - i * 0.35), 0)}
                </span>
              ))}
            </div>
            <svg
              className="equity-chart"
              viewBox={`-2 0 ${width + 4} ${height}`}
              preserveAspectRatio="none"
              onMouseMove={(e) => {
                const b = e.currentTarget.getBoundingClientRect();
                setHover(
                  Math.max(
                    0,
                    Math.min(
                      84,
                      Math.round(((e.clientX - b.left) / b.width) * 84),
                    ),
                  ),
                );
              }}
              onMouseLeave={() => setHover(null)}
              role="img"
              aria-label={`Illustrative portfolio value over ${range}`}
            >
              <defs>
                <linearGradient id="area" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#c5ee9c" stopOpacity=".17" />
                  <stop offset="100%" stopColor="#c5ee9c" stopOpacity="0" />
                </linearGradient>
              </defs>
              {[28, 83, 138, 193].map((y) => (
                <line
                  key={y}
                  x1="0"
                  x2={width}
                  y1={y}
                  y2={y}
                  stroke="#30342c"
                  strokeDasharray="3 5"
                />
              ))}
              <path d={`${line} L${width},224 L0,224 Z`} fill="url(#area)" />
              <path
                d={line}
                fill="none"
                stroke="#caeea0"
                strokeWidth="2.2"
                vectorEffect="non-scaling-stroke"
              />
              {hover != null && (
                <line
                  x1={pts[ix][0]}
                  x2={pts[ix][0]}
                  y1="10"
                  y2="224"
                  stroke="#929986"
                  strokeDasharray="3 4"
                />
              )}
              <circle
                cx={pts[ix][0]}
                cy={pts[ix][1]}
                r="4"
                fill="#dbf9bc"
                stroke="#22291c"
                strokeWidth="5"
                vectorEffect="non-scaling-stroke"
              />
            </svg>
            <div className="chart-x-labels">
              {(range === "1D"
                ? ["00:00", "04:00", "08:00", "12:00", "16:00", "20:00"]
                : [
                    "08 OCT",
                    "13 OCT",
                    "18 OCT",
                    "23 OCT",
                    "28 OCT",
                    "02 NOV",
                    "07 NOV",
                  ]
              ).map((v) => (
                <span key={v}>{v}</span>
              ))}
            </div>
          </>
        ) : (
          <Empty
            title="Equity history not measured"
            description="The console will not substitute an illustrative curve for a connected account. Trade history is available separately when ClickHouse is attached."
          />
        )}
      </div>
      <div className="chart-footer">
        <div>
          <span className="line-key" /> Portfolio value{" "}
          <span className="dim">
            · {demo ? "Sample workspace" : "Unavailable"}
          </span>
        </div>
        <span>
          Denominated in USD <Icon as={CircleHelp} size={12} />
        </span>
      </div>
    </section>
  );
}

export default function App() {
  const [page, setPage] = useState<Page>("overview");
  const [demo, setDemo] = useState(true);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [capabilities, setCapabilities] = useState<RuntimeCapabilities | null>(null);
  const [compatibility, setCompatibility] = useState("Runtime API not negotiated");
  const [apiError, setApiError] = useState(false);
  const [history, setHistory] = useState<Position[]>([]);
  const [historyError, setHistoryError] = useState(false);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [modal, setModal] = useState<Modal>(null);
  const [selected, setSelected] = useState<Position | null>(null);
  const [range, setRange] = useState("1M");
  const [exchange, setExchange] = useState("Binance");
  const [mode, setMode] = useState("Paper");
  const [query, setQuery] = useState("");
  const [journalSide, setJournalSide] = useState("All directions");
  const [toast, setToast] = useState("");
  const [menu, setMenu] = useState(false);
  const [sidebar, setSidebar] = useState(false);
  const [clock, setClock] = useState(new Date());
  const [researchTab, setResearchTab] = useState("Evaluation planner");
  const [percentile, setPercentile] = useState<
    "latency_p50_ms" | "latency_p95_ms" | "latency_p99_ms"
  >("latency_p95_ms");
  const [draft, setDraft] = useState(() => {
    try {
      return (
        JSON.parse(localStorage.getItem("aitos-draft-v1") || "null") || {
          topN: 5,
          interval: 10,
          risk: 1,
          leverage: 5,
        }
      );
    } catch {
      return { topN: 5, interval: 10, risk: 1, leverage: 5 };
    }
  });
  const [dirty, setDirty] = useState(false);
  const [installPrompt, setInstallPrompt] = useState<any>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    let stop = false;
    let controller: AbortController;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 8000);
      try {
        const contract = await consoleFetch("/console/v1/capabilities", { signal: controller.signal });
        if (!contract.ok) throw Error("Runtime capability negotiation unavailable");
        const negotiated = parseRuntimeCapabilities(await contract.json());
        const r = await consoleFetch("/console/v1/snapshot", {
          signal: controller.signal,
          cache: "no-store",
        });
        if (!r.ok) throw Error();
        const s = parseSnapshot(await r.json());
        if (!stop) {
          setSnapshot(s);
          setCapabilities(negotiated);
          setCompatibility("Read-only API 1.0 negotiated; availability is not health or trading authority");
          setApiError(false);
        }
      } catch {
        if (!stop) {
          setApiError(true);
          setSnapshot(null);
          setCapabilities(null);
          setCompatibility("Runtime API unavailable or incompatible — requires read-only protocol 1.0. No endpoint fallback or authority transfer.");
          setHistory([]);
          setSelected(null);
        }
      } finally {
        clearTimeout(timeout);
        if (!stop) timer = setTimeout(poll, 5000);
      }
    }
    poll();
    return () => {
      stop = true;
      controller?.abort();
      clearTimeout(timer);
    };
  }, []);
  useEffect(() => {
    if (!modal && !selected) return;
    const previous = document.activeElement as HTMLElement | null;
    const dialog = document.querySelector<HTMLElement>('[role="dialog"]');
    const focusable = () =>
      Array.from(
        dialog?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input, select, [tabindex="0"]',
        ) || [],
      );
    focusable()[0]?.focus();
    const trap = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const items = focusable();
      const first = items[0],
        last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", trap);
    return () => {
      document.removeEventListener("keydown", trap);
      previous?.focus();
    };
  }, [modal, selected]);
  useEffect(() => {
    if (demo || apiError || page !== "history") return;
    const c = new AbortController();
    const timeout = setTimeout(() => c.abort(), 8000);
    let disposed = false;
    setHistoryError(false);
    setHistoryLoading(true);
    setHistory([]);
    consoleFetch(`/console/v1/history?limit=100&offset=${historyOffset}`, {
      signal: c.signal,
      cache: "no-store",
    })
      .then((r) => {
        if (!r.ok) throw Error();
        return r.json();
      })
      .then((d) => {
        if (!disposed) setHistory(parseHistory(d));
      })
      .catch(() => {
        if (!disposed) {
          setHistory([]);
          setHistoryError(true);
        }
      })
      .finally(() => {
        clearTimeout(timeout);
        if (!disposed) setHistoryLoading(false);
      });
    return () => {
      disposed = true;
      clearTimeout(timeout);
      c.abort();
    };
  }, [demo, page, historyOffset, apiError]);
  useEffect(() => {
    const t = setInterval(() => setClock(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(""), 4200);
    return () => clearTimeout(t);
  }, [toast]);
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setPage("history");
        setTimeout(() => searchRef.current?.focus(), 30);
      }
      if (e.key === "Escape") {
        setModal(null);
        setSelected(null);
        setMenu(false);
      }
    };
    const install = (e: Event) => {
      e.preventDefault();
      setInstallPrompt(e);
    };
    window.addEventListener("keydown", key);
    window.addEventListener("beforeinstallprompt", install);
    return () => {
      window.removeEventListener("keydown", key);
      window.removeEventListener("beforeinstallprompt", install);
    };
  }, []);
  const rows = demo
    ? samplePositions
    : !apiError
      ? snapshot?.positions || []
      : [];
  const stages = demo
    ? sampleStages
    : !apiError
      ? snapshot?.pipeline || []
      : [];
  const journal = (demo ? sampleHistory : apiError ? [] : history).filter(
    (p) =>
      (p.symbol.toLowerCase().includes(query.toLowerCase()) ||
        p.trade_id.toLowerCase().includes(query.toLowerCase())) &&
      (journalSide === "All directions" ||
        p.side === journalSide.toUpperCase()),
  );
  const latency = stages.find((s) =>
    ["Trade lifecycle", "trade_lifecycle"].includes(s.stage),
  );
  const connected = !apiError && snapshot?.connected;
  const realized =
    journal.length && journal.every((p) => p.pnl !== null)
      ? journal.reduce((n, p) => n + (p.pnl ?? 0), 0)
      : null;
  const title = navigation.find((n) => n.id === page)!;
  const navigate = (id: Page) => {
    setPage(id);
    setSidebar(false);
    setQuery("");
  };
  const notice = (s: string) => setToast(s);
  const exportJournal = () => {
    if (!journal.length) return notice("No history rows to export.");
    exportCsv(
      journal.map((p) => ({
        source: demo ? "SAMPLE DATA" : "ClickHouse",
        id: p.trade_id,
        symbol: p.symbol,
        side: p.side,
        pnl: p.pnl,
        entry_time: p.entry_time,
        exit_time: p.exit_time,
      })),
      `aitos-${demo ? "sample-" : ""}journal.csv`,
    );
    notice("Journal exported. Source labels are included.");
  };
  const draftValid = checkDraft(draft);
  function saveDraft() {
    if (!draftValid)
      return notice("Check the highlighted configuration bounds.");
    try {
      localStorage.setItem("aitos-draft-v1", JSON.stringify(draft));
    } catch {
      return notice("Device storage is unavailable. Draft was not saved.");
    }
    setDirty(false);
    notice(
      "Draft saved on this device. Runtime configuration was not changed.",
    );
  }
  const exportDraft = () => {
    if (!draftValid)
      return notice("Configuration is outside validated bounds.");
    download(
      new Blob(
        [
          JSON.stringify(
            {
              kind: "UNAPPLIED_CONFIGURATION_DRAFT",
              mode: "paper",
              scanner: {
                top_n: draft.topN,
                refresh_interval_seconds: draft.interval,
              },
              risk: {
                max_risk_per_trade_pct: draft.risk,
                max_leverage: draft.leverage,
              },
            },
            null,
            2,
          ),
        ],
        { type: "application/json" },
      ),
      "aitos-paper-draft.json",
    );
    notice("Unapplied paper configuration draft exported.");
  };
  return (
    <div className="app-shell">
      <aside className={`sidebar ${sidebar ? "mobile-open" : ""}`}>
        <button
          className="brand"
          onClick={() => navigate("overview")}
          aria-label="AITOS home"
        >
          <img src="/icon.svg" alt="" />
          <span>
            aitos<span className="brand-dot">.</span>
          </span>
          <span className="brand-edition">OS</span>
        </button>
        <div className="workspace">
          <div className="workspace-icon">
            <Layers3 size={18} />
          </div>
          <div>
            <strong>Personal workspace</strong>
            <small>Operator console</small>
          </div>
          <LockKeyhole size={12} />
        </div>
        <div className="nav-label">
          WORKSPACE{" "}
          <span>01 — {String(navigation.length).padStart(2, "0")}</span>
        </div>
        <nav>
          {navigation.map(({ id, label, icon }, i) => (
            <button
              key={id}
              className={`nav-item ${page === id ? "active" : ""}`}
              onClick={() => navigate(id)}
            >
              <Icon as={icon} size={18} />
              <span>{label}</span>
              {id === "positions" ? (
                <span className="nav-count">{rows.length}</span>
              ) : (
                <span className="nav-index">
                  {String(i + 1).padStart(2, "0")}
                </span>
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="operator-note">
            <ShieldCheck size={20} />
            <strong>Designed for control.</strong>
            <p>
              Every decision has context.
              <br />
              Every execution leaves a trace.
            </p>
            <button onClick={() => setModal("help")}>
              Read the operating guide <ArrowUpRight size={13} />
            </button>
          </div>
          <button className="sidebar-link" onClick={() => setModal("help")}>
            <CircleHelp size={16} />
            Help & documentation
            <ArrowUpRight size={13} />
          </button>
          <div className="profile">
            <span className="avatar">FH</span>
            <div>
              <strong>Operator</strong>
              <small>Local workspace</small>
            </div>
            <span className="status-dot" />
          </div>
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <button
            className="mobile-toggle icon-button"
            onClick={() => setSidebar(!sidebar)}
            aria-label="Toggle navigation"
          >
            <Layers3 size={19} />
          </button>
          <div className="breadcrumb">
            Workspace <ChevronRight size={12} />
            <span>{title.label}</span>
          </div>
          <div className="topbar-right">
            <span className="utc mono">
              {clock.toLocaleTimeString("en-GB", { timeZone: "UTC" })}{" "}
              <span>UTC</span>
            </span>
            <button
              className="source-switch"
              onClick={() => {
                setDemo(!demo);
                notice(
                  demo
                    ? "Runtime view selected. Unavailable metrics remain blank."
                    : "Sample workspace selected. All financial values are illustrative.",
                );
              }}
            >
              <span className={`status-dot ${demo ? "amber" : ""}`} />
              {demo ? "Sample workspace" : "Runtime view"}
              <ChevronDown size={12} />
            </button>
            <span className="header-divider" />
            <button
              className="icon-button notification-button"
              onClick={() => setModal("notifications")}
              aria-label="Notifications"
            >
              <Bell size={17} />
              <span />
            </button>
            <button
              className="icon-button"
              onClick={() => setModal("help")}
              aria-label="Help"
            >
              <CircleHelp size={17} />
            </button>
          </div>
        </header>
        <main>
          <div className="page-heading">
            <div>
              <div className="eyebrow">
                <span className="mini-line" />{" "}
                {page === "overview"
                  ? "THE OPERATOR’S PERSPECTIVE"
                  : `WORKSPACE / ${title.label.toUpperCase()}`}
              </div>
              <h1>
                {page === "overview" ? "Capital, clearly" : title.label}
                <span className="heading-dot">.</span>
              </h1>
              <p>
                {
                  {
                    overview:
                      "Your capital, positions and execution. One considered view.",
                    positions:
                      "Every position. Every boundary. Nothing out of sight.",
                    history:
                      "The decisions behind the outcomes. A journal, not just a ledger.",
                    research: "Challenge the hypothesis. Protect the holdout.",
                    telemetry:
                      "See the path from market event to measured decision.",
                    exchanges:
                      "Your venues and accounts, with clear capability boundaries.",
                    settings:
                      "Considered parameters. Explicit changes. No hidden defaults.",
                    performance:
                      "Recorded outcomes. Clear costs. Honest boundaries.",
                    readiness:
                      "Evidence-led decisions. Explicit unknowns. A safer next step.",
                    persistence:
                      "Committed state. Verified delivery. Explicit recovery boundaries.",
                  }[page]
                }
              </p>
            </div>
            <div className="page-actions">
              <div className="select-wrap">
                <Globe size={14} />
                <select
                  aria-label="Workspace exchange"
                  value={exchange}
                  onChange={(e) => {
                    setExchange(e.target.value);
                    notice(
                      "Workspace selection changed. Runtime exchange was not modified.",
                    );
                  }}
                >
                  {["Binance", "Alfax", "cTrader", "MT5", "TradeLocker"].map(
                    (x) => (
                      <option key={x}>{x}</option>
                    ),
                  )}
                </select>
                <ChevronDown size={12} />
              </div>
              <div className="mode-select">
                <button
                  className={mode === "Paper" ? "active" : ""}
                  onClick={() => {
                    setMode("Paper");
                    notice(
                      "Paper workspace selected; no runtime was launched.",
                    );
                  }}
                >
                  Paper
                </button>
                <button
                  className={mode === "Backtest" ? "active" : ""}
                  onClick={() => {
                    setMode("Backtest");
                    navigate("research");
                  }}
                >
                  Backtest
                </button>
                <button onClick={() => setModal("live")}>
                  <LockKeyhole size={11} /> Live
                </button>
              </div>
            </div>
          </div>
          <div className={`context-strip ${demo ? "demo-strip" : ""}`}>
            <div>
              <span
                className={`status-dot ${demo ? "amber" : connected ? "" : "off"}`}
              />
              <strong>
                {demo
                  ? "DESIGN PREVIEW"
                  : connected
                    ? "RUNTIME ATTACHED"
                    : "NO RUNTIME ATTACHED"}
              </strong>
              <span>
                {demo
                  ? "Illustrative account, positions and measurements. No orders can be placed."
                  : connected
                    ? `Declared runtime: ${snapshot?.mode || "unknown"}. Read-only telemetry; configuration and execution remain gated.`
                    : "Start an approved backend and attach its read-only console API. Values are not inferred."}
              </span>
            </div>
            <button
              onClick={() => (demo ? setDemo(false) : setModal("connection"))}
            >
              {demo ? "View actual runtime" : "Connection details"}
              <ArrowRight size={13} />
            </button>
          </div>
          {page === "performance" && (
            <PerformancePanel bridgeFailed={apiError} />
          )}
          {page === "persistence" && (
            <PersistencePanel snapshot={apiError ? null : snapshot} />
          )}
          {page === "readiness" && (
            <ReadinessPanel snapshot={apiError ? null : snapshot} />
          )}
          {page === "overview" && (
            <>
              <div className="stats-grid">
                <div className="stat">
                  <div className="stat-label">
                    Net account value <Wallet size={15} />
                  </div>
                  <div className="stat-value">
                    {demo ? money(128450.25) : "—"}
                    <span className="currency">USD</span>
                  </div>
                  <div className="stat-foot">
                    <Pill tone="green">{demo ? "+7.72%" : "Unavailable"}</Pill>
                    <span>
                      {demo
                        ? "vs. last month · sample"
                        : "Account balance feed not attached"}
                    </span>
                  </div>
                </div>
                <div className="stat">
                  <div className="stat-label">
                    Realized P&L <TrendingUp size={15} />
                  </div>
                  <div className="stat-value positive">
                    {demo ? "+$842.00" : "—"}
                  </div>
                  <div className="stat-foot">
                    <span className="stat-tick positive">
                      <ArrowUpRight size={13} />
                      {demo ? "Today" : "Not measured"}
                    </span>
                    <span>
                      {demo
                        ? "6 sample closed trades"
                        : "Open the journal for persisted history"}
                    </span>
                  </div>
                </div>
                <div className="stat">
                  <div className="stat-label">
                    Open positions <Layers3 size={15} />
                  </div>
                  <div className="stat-value">
                    {rows.length}
                    <span className="stat-denominator">
                      {" "}
                      / {demo ? "3" : "—"}
                    </span>
                  </div>
                  <div className="stat-foot">
                    <div className="mini-coins">
                      {rows.slice(0, 3).map((p) => (
                        <Coin key={p.trade_id} symbol={p.symbol} />
                      ))}
                    </div>
                    <span>
                      {demo
                        ? "Across 3 instruments"
                        : connected
                          ? "From runtime lifecycle"
                          : "Runtime unavailable"}
                    </span>
                  </div>
                </div>
                <div className="stat">
                  <div className="stat-label">
                    Lifecycle latency <Zap size={15} />
                  </div>
                  <div className="stat-value">
                    {latency?.latency_sample_count
                      ? number(latency.latency_p95_ms)
                      : "—"}
                    <span className="stat-denominator"> ms</span>
                  </div>
                  <div className="stat-foot">
                    <Pill tone={demo ? "green" : "muted"}>
                      {latency?.latency_sample_count ? "p95" : "No samples"}
                    </Pill>
                    <span>
                      {demo
                        ? "240 illustrative samples"
                        : "Process-local stage measurement"}
                    </span>
                  </div>
                </div>
              </div>
              <div className="overview-middle">
                <EquityChart demo={demo} range={range} onRange={setRange} />
                <section className="panel posture">
                  <div className="panel-heading">
                    <h2>Risk posture</h2>
                    <ShieldCheck size={16} />
                  </div>
                  <div className="risk-orbit">
                    <svg
                      viewBox="0 0 240 140"
                      role="img"
                      aria-label={
                        demo
                          ? "Illustrative risk utilization of 22 percent"
                          : "Risk utilization unavailable"
                      }
                    >
                      <path
                        d="M25 120 A95 95 0 0 1 215 120"
                        fill="none"
                        stroke="#34392f"
                        strokeWidth="10"
                        strokeLinecap="round"
                      />
                      <path
                        d="M25 120 A95 95 0 0 1 215 120"
                        fill="none"
                        stroke="#c8eb9e"
                        strokeWidth="10"
                        strokeLinecap="round"
                        strokeDasharray={demo ? "66 298" : "0 298"}
                      />
                      <text
                        x="120"
                        y="103"
                        textAnchor="middle"
                        fill="#f0f2e8"
                        fontSize="42"
                        fontFamily="Manrope"
                      >
                        {demo ? "22" : "—"}
                        <tspan fontSize="19" fill="#8d9587">
                          %
                        </tspan>
                      </text>
                      <text
                        x="120"
                        y="129"
                        textAnchor="middle"
                        fill="#8d9587"
                        fontSize="10"
                        letterSpacing="1.4"
                      >
                        RISK BUDGET USED
                      </text>
                    </svg>
                  </div>
                  <div className="risk-caption">
                    <span className="status-dot" />
                    {demo
                      ? "Within sample boundaries"
                      : "Limits require a connected runtime"}
                  </div>
                  <div className="risk-metrics">
                    <div>
                      <span>Portfolio drawdown</span>
                      <strong>
                        {demo ? "1.24%" : "—"}
                        <small> / {demo ? "10%" : "—"}</small>
                      </strong>
                    </div>
                    <div className="risk-track">
                      <span style={{ width: demo ? "12.4%" : "0%" }} />
                    </div>
                    <div>
                      <span>Allocated capital</span>
                      <strong>{demo ? "38.6%" : "—"}</strong>
                    </div>
                    <div className="risk-track neutral">
                      <span style={{ width: demo ? "38.6%" : "0%" }} />
                    </div>
                  </div>
                  <button
                    className="panel-link"
                    onClick={() => navigate("settings")}
                  >
                    Review risk parameters <ArrowRight size={13} />
                  </button>
                </section>
              </div>
              <div className="overview-bottom">
                <section className="panel positions-panel">
                  <div className="panel-heading">
                    <div className="inline">
                      <h2>Open positions</h2>
                      <span className="count-box">{rows.length}</span>
                    </div>
                    <button
                      className="text-button"
                      onClick={() => navigate("positions")}
                    >
                      View portfolio <ArrowUpRight size={14} />
                    </button>
                  </div>
                  <PositionTable rows={rows} compact onSelect={setSelected} />
                  <div className="table-note">
                    <span className="status-dot" />
                    {demo
                      ? "Sample position ledger"
                      : "Runtime lifecycle snapshot"}
                    <span>Reduce-only execution · protective boundaries</span>
                  </div>
                </section>
                <section className="panel market-panel">
                  <div className="panel-heading">
                    <h2>Market lens</h2>
                    <button
                      className="icon-button"
                      onClick={() =>
                        notice(
                          "Market lens is illustrative. A measured price-feed adapter is not attached.",
                        )
                      }
                      aria-label="Market data details"
                    >
                      <MoreHorizontal size={17} />
                    </button>
                  </div>
                  {demo ? (
                    markets.slice(0, 4).map((m, i) => (
                      <div className="market-row" key={m.symbol}>
                        <Coin symbol={m.symbol} />
                        <div className="market-name">
                          <strong>
                            {m.symbol}
                            <small>{m.name}</small>
                          </strong>
                        </div>
                        <Sparkline positive={m.change > 0} seed={i} />
                        <div className="market-price">
                          <strong>
                            {money(m.price, m.price > 1000 ? 0 : 2)}
                          </strong>
                          <small
                            className={m.change > 0 ? "positive" : "negative"}
                          >
                            {m.change > 0 ? "+" : ""}
                            {m.change}%
                          </small>
                        </div>
                      </div>
                    ))
                  ) : (
                    <Empty
                      title="No market feed"
                      description="A live price feed is not attached to this console."
                    />
                  )}
                  <div className="market-caption">
                    {demo ? "ILLUSTRATIVE PRICES" : "NO PRICE SAMPLES"}
                    <span>USDT pairs</span>
                  </div>
                </section>
              </div>
              <div className="bottom-ribbon">
                <span>
                  <ShieldCheck size={14} /> Execution is gated. Your intent
                  stays explicit.
                </span>
                <button onClick={() => setModal("launch")}>
                  Prepare a paper session <ArrowRight size={14} />
                </button>
              </div>
            </>
          )}
          {page === "positions" && (
            <>
              <div className="section-toolbar">
                <div>
                  <Pill tone="green">{rows.length} open positions</Pill>
                  <span className="dim">
                    {" "}
                    {demo ? "Sample portfolio" : "Lifecycle snapshot"}
                  </span>
                </div>
                <button
                  className="button secondary"
                  onClick={() =>
                    exportCsv(
                      rows.map((p) => ({
                        ...p,
                        source: demo ? "SAMPLE DATA" : "Runtime",
                      })),
                      `aitos-${demo ? "sample-" : ""}positions.csv`,
                    )
                  }
                >
                  <ArrowDownToLine size={14} />
                  Export positions
                </button>
              </div>
              <section className="panel">
                <div className="panel-heading">
                  <h2>Position ledger</h2>
                  <span className="mono dim">
                    {demo ? "SAMPLE" : "READ ONLY"}
                  </span>
                </div>
                <PositionTable rows={rows} onSelect={setSelected} />
              </section>
              <div className="two-columns">
                <section className="panel padded">
                  <div className="inline">
                    <ShieldCheck size={18} />
                    <h2>Protection is not optional.</h2>
                  </div>
                  <p className="body-copy">
                    Every actual position must have durable ownership, confirmed
                    quantity and a protective boundary. A submission
                    acknowledgement is never treated as a full fill.
                  </p>
                  <div className="checks">
                    <span>
                      <Check size={14} /> Reduce-only reductions
                    </span>
                    <span>
                      <Check size={14} /> Explicit uncertainty quarantine
                    </span>
                    <span>
                      <Check size={14} /> Account snapshot verification
                    </span>
                  </div>
                </section>
                <section className="panel padded">
                  <h2>Exposure by instrument</h2>
                  {rows.map((p) => (
                    <div className="exposure" key={p.trade_id}>
                      <div>
                        <strong>{p.symbol}</strong>
                        <span>{money(p.position_size_usd)}</span>
                      </div>
                      <div className="risk-track">
                        <span
                          style={{
                            width: `${(p.position_size_usd / Math.max(1, ...rows.map((r) => r.position_size_usd))) * 100}%`,
                          }}
                        />
                      </div>
                    </div>
                  ))}
                </section>
              </div>
            </>
          )}
          {page === "history" && (
            <>
              <div className="journal-summary">
                <span className="small-cap">
                  {demo ? "SAMPLE JOURNAL" : "PERSISTED TRADE HISTORY"}
                </span>
                <h2 className={(realized ?? 0) >= 0 ? "positive" : "negative"}>
                  {money(realized)}
                </h2>
                <span>
                  Realized P&L across {journal.length} loaded{" "}
                  {demo ? "sample " : ""}trades · not an all-time account total
                </span>
              </div>
              <section className="panel">
                <div className="panel-heading wrapping">
                  <div className="search-field">
                    <Search size={15} />
                    <input
                      ref={searchRef}
                      aria-label="Search trade journal"
                      placeholder="Search instrument or trade ID…"
                      value={query}
                      onChange={(e) => setQuery(e.target.value)}
                    />
                    <kbd>⌘ K</kbd>
                  </div>
                  <div className="inline">
                    <select
                      className="plain-select"
                      aria-label="Filter trade direction"
                      value={journalSide}
                      onChange={(e) => setJournalSide(e.target.value)}
                    >
                      <option>All directions</option>
                      <option>Long</option>
                      <option>Short</option>
                    </select>
                    <button
                      className="button secondary"
                      onClick={exportJournal}
                    >
                      <ArrowDownToLine size={14} />
                      Export CSV
                    </button>
                  </div>
                </div>
                {historyLoading && !demo ? (
                  <Empty
                    title="Loading persisted history"
                    description="Querying a bounded batch from the real history adapter. No previous batch is shown while loading."
                  />
                ) : historyError && !demo ? (
                  <Empty
                    title="History store unavailable"
                    description="The real ClickHouse history adapter has not been attached, or its query failed. No sample records are substituted."
                  />
                ) : journal.length ? (
                  <div className="table-scroll">
                    <table>
                      <thead>
                        <tr>
                          <th>Instrument</th>
                          <th>Direction</th>
                          <th>Entry</th>
                          <th>Exit</th>
                          <th>Realized P&L</th>
                          <th>Closed at (UTC)</th>
                          <th>Source</th>
                        </tr>
                      </thead>
                      <tbody>
                        {journal.map((p) => (
                          <tr key={p.trade_id} onClick={() => setSelected(p)}>
                            <td>
                              <div className="instrument">
                                <Coin symbol={p.symbol} />
                                <strong>{p.symbol}</strong>
                              </div>
                            </td>
                            <td
                              className={
                                p.side === "LONG" ? "positive" : "negative"
                              }
                            >
                              {p.side}
                            </td>
                            <td className="mono">{money(p.entry_price)}</td>
                            <td className="mono">{money(p.exit_price)}</td>
                            <td
                              className={`mono ${(p.pnl || 0) >= 0 ? "positive" : "negative"}`}
                            >
                              {money(p.pnl)}
                            </td>
                            <td className="mono dim">
                              {p.exit_time
                                ? new Date(p.exit_time).toLocaleString(
                                    "en-GB",
                                    { timeZone: "UTC" },
                                  )
                                : "—"}
                            </td>
                            <td>
                              <Pill>{demo ? "Sample" : "ClickHouse"}</Pill>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : (
                  <Empty
                    title="No matching trades"
                    description="Change your search or direction filter. Runtime history requires the authenticated read-only ClickHouse adapter."
                  />
                )}
                <div className="table-note">
                  <Database size={13} />
                  {demo
                    ? "Illustrative records. Switch to Runtime view for persisted data."
                    : `Read-only batch ${Math.floor(historyOffset / 100) + 1} · ${history.length} loaded records · search filters this batch only`}
                </div>
              </section>
              {!demo && (
                <div className="section-toolbar">
                  <span className="dim">
                    Offset pagination is not a stable snapshot while the ledger
                    changes.
                  </span>
                  <div className="inline">
                    <button
                      className="button secondary"
                      disabled={historyLoading || historyOffset === 0}
                      onClick={() =>
                        setHistoryOffset(Math.max(0, historyOffset - 100))
                      }
                    >
                      <ChevronLeft size={14} />
                      Previous batch
                    </button>
                    <button
                      className="button secondary"
                      disabled={
                        historyLoading ||
                        historyError ||
                        history.length < 100 ||
                        historyOffset >= 100000
                      }
                      onClick={() => setHistoryOffset(historyOffset + 100)}
                    >
                      Next batch
                      <ChevronRight size={14} />
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
          {page === "telemetry" && (
            <>
              <div className="section-toolbar">
                <div className="inline">
                  <Radio size={17} />
                  <span>Market event → execution lifecycle</span>
                  <Pill>
                    {demo ? "Sample measurements" : "Process-local registry"}
                  </Pill>
                </div>
                <div className="segmented">
                  {(
                    [
                      "latency_p50_ms",
                      "latency_p95_ms",
                      "latency_p99_ms",
                    ] as const
                  ).map((p, i) => (
                    <button
                      key={p}
                      className={percentile === p ? "active" : ""}
                      onClick={() => setPercentile(p)}
                    >
                      {["p50", "p95", "p99"][i]}
                    </button>
                  ))}
                </div>
              </div>
              <section className="panel">
                <div className="panel-heading">
                  <h2>The execution path</h2>
                  <span className="dim">
                    Latency distributions, not averages of percentiles
                  </span>
                </div>
                {stages.length ? (
                  <div className="pipeline-chart">
                    {stages.map((s, i) => (
                      <div className="pipeline-row" key={s.stage}>
                        <span className="pipeline-index mono">
                          {String(i + 1).padStart(2, "0")}
                        </span>
                        <strong>{s.stage.replaceAll("_", " ")}</strong>
                        <div className="pipeline-track">
                          <span
                            style={{
                              width: s.latency_sample_count
                                ? `${Math.max(1, ((s[percentile] || 0) / Math.max(1, ...stages.map((s) => s[percentile] || 0))) * 100)}%`
                                : "0%",
                            }}
                          />
                        </div>
                        <span className="mono">
                          {s.latency_sample_count
                            ? `${number(s[percentile])} ms`
                            : "Not measured"}
                        </span>
                        <span className="dim mono">
                          {number(s.latency_sample_count, 0)} samples
                        </span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <Empty
                    title="No measured stage samples"
                    description="Attach the runtime telemetry registry. Missing measurements are never displayed as zero latency."
                  />
                )}
                <div className="table-note">
                  <CircleHelp size={13} />
                  Stage processing time is not exchange network round-trip time.
                  Empty windows remain unmeasured.
                </div>
              </section>
              <div className="two-columns">
                <section className="panel padded">
                  <div className="inline">
                    <Box size={17} />
                    <h2>Runtime modules</h2>
                  </div>
                  {(demo
                    ? [
                        { name: "market-data", status: "sample healthy" },
                        { name: "event-bus", status: "sample healthy" },
                        { name: "trade-lifecycle", status: "sample healthy" },
                        { name: "persistence", status: "sample healthy" },
                      ]
                    : snapshot?.modules || []
                  ).map((m) => (
                    <div className="module-row" key={m.name}>
                      <span>{m.name}</span>
                      <Pill
                        tone={m.status.includes("healthy") ? "green" : "muted"}
                      >
                        {m.status}
                      </Pill>
                    </div>
                  ))}
                </section>
                <section className="panel padded">
                  <h2>Measurement integrity</h2>
                  <p className="body-copy">
                    Percentiles use the existing bounded stage-sample registry.
                    Queue depth, sample count and error counters stay separate.
                    No synthetic heartbeat is presented as market execution
                    latency.
                  </p>
                  <div className="notice-box">
                    <LockKeyhole size={16} />
                    <span>
                      End-to-end production latency and exchange round-trip
                      measurements are not certified.
                    </span>
                  </div>
                </section>
              </div>
            </>
          )}
          {page === "research" && (
            <>
              <section className="research-hero panel">
                <div>
                  <span className="small-cap">EVIDENCE BEFORE CONVICTION</span>
                  <h2>
                    A better question.
                    <br />A stronger decision.
                  </h2>
                  <p>
                    Keep development, walk-forward windows and locked holdouts
                    separate. Nothing promotes itself to live.
                  </p>
                  <button
                    className="button primary"
                    onClick={() => setModal("launch")}
                  >
                    <Plus size={15} />
                    Prepare research draft
                  </button>
                </div>
                <div className="research-art" aria-hidden="true">
                  {Array.from({ length: 32 }, (_, i) => (
                    <span
                      key={i}
                      style={{
                        height: `${30 + Math.sin(i * 0.3) * 24 + i * 1.7}%`,
                        opacity: 0.2 + i / 45,
                      }}
                    />
                  ))}
                  <span className="art-caption mono">
                    HYPOTHESIS → VALIDATION → EVIDENCE
                  </span>
                </div>
              </section>
              <div className="section-toolbar">
                <div className="tabs">
                  {[
                    "Evaluation planner",
                    "Experiments",
                    "Validation gates",
                    "Model governance",
                  ].map((t) => (
                    <button
                      key={t}
                      className={researchTab === t ? "active" : ""}
                      onClick={() => setResearchTab(t)}
                    >
                      {t}
                    </button>
                  ))}
                </div>
                <Pill>Execution not attached</Pill>
              </div>
              {researchTab === "Evaluation planner" ? (
                <ResearchPlanner />
              ) : researchTab === "Experiments" ? (
                <section className="panel">
                  <Empty
                    title="Make the evidence count."
                    description="Backtest job submission and durable experiment retrieval are not wired to this console yet. Prepare an unapplied configuration draft; no fabricated experiment results are shown."
                  />
                  <div className="research-actions">
                    <button
                      className="button secondary"
                      onClick={() => navigate("settings")}
                    >
                      <Settings2 size={15} />
                      Configure a draft
                    </button>
                    <button
                      className="text-button"
                      onClick={() => setResearchTab("Validation gates")}
                    >
                      Explore the validation gates <ArrowRight size={14} />
                    </button>
                  </div>
                </section>
              ) : (
                <div className="gate-grid">
                  {(researchTab === "Validation gates"
                    ? [
                        "Development integrity",
                        "Walk-forward evaluation",
                        "Locked holdout",
                        "Numerical evidence",
                      ]
                    : [
                        "Frozen candidate identity",
                        "Champion comparison",
                        "Approval audit",
                        "Explicit promotion",
                      ]
                  ).map((t, i) => (
                    <section className="panel padded" key={t}>
                      <span className="step-no mono">0{i + 1}</span>
                      <h2>{t}</h2>
                      <p className="body-copy">
                        {
                          [
                            "Reject missing, non-finite or contaminated evidence.",
                            "Evaluate without retuning on unseen sequential windows.",
                            "Keep out-of-sample observations outside the evolution loop.",
                            "No automatic live activation from a passing backtest.",
                          ][i]
                        }
                      </p>
                      <Pill tone="green">Backend guard implemented</Pill>
                    </section>
                  ))}
                </div>
              )}
            </>
          )}
          {page === "exchanges" && !demo && <><AccountPanel /><ProtectionPanel /></>}
          {page === "exchanges" && (
            <section className="panel" aria-label="Runtime API capabilities">
              <div className="panel-heading"><h3>Runtime protocol & capabilities</h3></div>
              <p>{compatibility}</p>
              <p className="muted">Endpoint selection belongs to the native manager. Backend credentials never enter this UI. Missing capabilities remain unavailable, not sample balances or enabled controls.</p>
              {capabilities && <div className="gate-grid">{capabilities.capabilities.map(c =>
                <div className="panel" key={c.id}><strong>{c.id}</strong><p>{c.available ? "Adapter available — inspect evidence freshness" : "Not connected / not supported"}</p></div>
              )}</div>}
            </section>
          )}
          {page === "exchanges" && (
            <>
              <section className="connection-banner">
                <div className="connection-icon">
                  <Globe size={25} />
                </div>
                <div>
                  <h2>One workspace. Explicit connections.</h2>
                  <p>
                    Venue selection is not account activation. Credentials never
                    belong in browser storage.
                  </p>
                </div>
                <button
                  className="button primary"
                  onClick={() => setModal("connection")}
                >
                  <Plus size={15} />
                  Connection requirements
                </button>
              </section>
              <div className="connection-grid">
                {[
                  {
                    name: "Binance",
                    kind: "USDⓈ-M Futures",
                    letter: "B",
                    supported: true,
                  },
                  {
                    name: "Alfax",
                    kind: "Binance-compatible transport",
                    letter: "A",
                    supported: false,
                  },
                  {
                    name: "cTrader",
                    kind: "Open API · position-bound execution",
                    letter: "c",
                    supported: false,
                  },
                  {
                    name: "MetaTrader 5",
                    kind: "Isolated Windows gateway",
                    letter: "M",
                    supported: false,
                  },
                  {
                    name: "TradeLocker",
                    kind: "Account-routed execution",
                    letter: "T",
                    supported: false,
                  },
                ].map((v) => (
                  <section className="panel venue-card" key={v.name}>
                    <div className="venue-top">
                      <span
                        className={`venue-mark ${v.name === "Binance" ? "binance" : ""}`}
                      >
                        {v.letter}
                      </span>
                      <Pill tone={v.supported ? "green" : "muted"}>
                        {v.supported ? "Guarded adapter" : "Not live-ready"}
                      </Pill>
                    </div>
                    <h2>{v.name}</h2>
                    <p>{v.kind}</p>
                    <div className="venue-detail">
                      <span className="status-dot off" />
                      No account connected in this console
                    </div>
                    <button
                      className="button secondary"
                      onClick={() => {
                        setExchange(v.name === "MetaTrader 5" ? "MT5" : v.name);
                        setModal("connection");
                      }}
                    >
                      Review connection <ArrowUpRight size={14} />
                    </button>
                  </section>
                ))}
              </div>
            </>
          )}
          {page === "settings" && (
            <>
              <div className="section-toolbar">
                <div className="inline">
                  <Pill tone="amber">LOCAL DRAFT</Pill>
                  <span className="dim">
                    No runtime changes are applied from this screen.
                  </span>
                </div>
                <button
                  className="text-button"
                  onClick={() => {
                    setDraft({ topN: 5, interval: 10, risk: 1, leverage: 5 });
                    setDirty(true);
                  }}
                >
                  Reset draft
                </button>
              </div>
              <div className="configuration-layout">
                <section className="panel padded">
                  <div className="panel-heading flush">
                    <div>
                      <h2>Decision parameters</h2>
                      <p className="body-copy">
                        Prepare a bounded configuration for review.
                      </p>
                    </div>
                    <SlidersHorizontal size={19} />
                  </div>
                  {[
                    {
                      key: "topN",
                      label: "Candidate shortlist",
                      hint: "Maximum ranked opportunities per scan",
                      unit: "instruments",
                      min: 1,
                      max: 1000,
                      step: 1,
                    },
                    {
                      key: "interval",
                      label: "Scanner interval",
                      hint: "Refresh cadence · calculations are not skipped",
                      unit: "seconds",
                      min: 0.1,
                      max: 3600,
                      step: 1,
                    },
                    {
                      key: "risk",
                      label: "Risk per trade ceiling",
                      hint: "Cannot exceed the backend’s 1% hard ceiling",
                      unit: "% equity",
                      min: 0.01,
                      max: 1,
                      step: 0.05,
                    },
                    {
                      key: "leverage",
                      label: "Leverage ceiling",
                      hint: "A ceiling, not a target allocation",
                      unit: "×",
                      min: 1,
                      max: 10,
                      step: 1,
                    },
                  ].map((f) => (
                    <label className="config-field" key={f.key}>
                      <div>
                        <strong>{f.label}</strong>
                        <small>{f.hint}</small>
                      </div>
                      <div className="number-input">
                        <input
                          aria-label={f.label}
                          type="number"
                          min={f.min}
                          max={f.max}
                          step={f.step}
                          value={draft[f.key]}
                          onChange={(e) => {
                            setDraft({
                              ...draft,
                              [f.key]:
                                e.target.value === ""
                                  ? ""
                                  : Number(e.target.value),
                            });
                            setDirty(true);
                          }}
                        />
                        <span>{f.unit}</span>
                      </div>
                    </label>
                  ))}
                  {!draftValid && (
                    <div className="validation-error" role="alert">
                      Enter finite values within the displayed bounds. Candidate
                      shortlist must be an integer.
                    </div>
                  )}
                  <div className="config-footer">
                    <span>
                      {dirty
                        ? "Unsaved changes"
                        : "Draft is local to this device"}
                    </span>
                    <button
                      className="button primary"
                      onClick={saveDraft}
                      disabled={!draftValid}
                    >
                      <Check size={15} />
                      Save local draft
                    </button>
                  </div>
                </section>
                <div>
                  <section className="panel padded">
                    <div className="inline">
                      <LockKeyhole size={18} />
                      <h2>Change, with intention.</h2>
                    </div>
                    <p className="body-copy">
                      Saving here does not reload a process, alter strategy
                      semantics, raise risk limits or switch an exchange
                      account. Runtime mutation requires a separately approved,
                      authenticated control plane.
                    </p>
                    <button
                      className="button secondary full"
                      disabled={!draftValid}
                      onClick={exportDraft}
                    >
                      <ArrowDownToLine size={15} />
                      Export unapplied draft
                    </button>
                  </section>
                  <section className="panel padded config-runtime">
                    <span className="small-cap">
                      EFFECTIVE BACKEND CONFIGURATION
                    </span>
                    <p className="body-copy">
                      {snapshot
                        ? "Read from the console API. An unattached console reports resolved configuration, not a running process."
                        : "The configuration adapter is unavailable."}
                    </p>
                    <details>
                      <summary>
                        Inspect read-only configuration{" "}
                        <ChevronDown size={13} />
                      </summary>
                      <pre>
                        {JSON.stringify(snapshot?.configuration || {}, null, 2)}
                      </pre>
                    </details>
                  </section>
                </div>
              </div>
            </>
          )}
          <footer className="page-footer">
            <span>
              <span className={`status-dot ${apiError ? "off" : ""}`} />
              {apiError
                ? "Console API unavailable"
                : snapshot
                  ? "Local console API reachable"
                  : "Connecting to console API"}
            </span>
            <span>
              AITOS OS <span className="dim"> / </span> LOCAL-FIRST{" "}
              <span className="dim"> / </span> v0.1.0
            </span>
            <button
              onClick={async () => {
                if (installPrompt) {
                  await installPrompt.prompt();
                  setInstallPrompt(null);
                } else
                  notice(
                    "Install via your browser’s app menu on a supported HTTPS/localhost origin. Native packages are not built yet.",
                  );
              }}
            >
              <ArrowDownToLine size={12} />
              Install workspace
            </button>
          </footer>
        </main>
      </div>
      {toast && (
        <div className="toast" role="status">
          <Check size={16} />
          <span>{toast}</span>
          <button
            aria-label="Dismiss notification"
            onClick={() => setToast("")}
          >
            <X size={14} />
          </button>
        </div>
      )}
      {selected && (
        <div className="modal-backdrop" onClick={() => setSelected(null)}>
          <section
            className="dialog"
            role="dialog"
            aria-modal="true"
            aria-label="Position details"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="dialog-top">
              <Pill>{demo ? "Sample position" : "Runtime record"}</Pill>
              <button
                className="icon-button"
                aria-label="Close details"
                onClick={() => setSelected(null)}
              >
                <X size={19} />
              </button>
            </div>
            <div className="inline dialog-instrument">
              <Coin symbol={selected.symbol} />
              <div>
                <h2>{selected.symbol}</h2>
                <span className="dim">
                  {selected.side} · {selected.state.replaceAll("_", " ")}
                </span>
              </div>
            </div>
            <div className="detail-grid">
              {[
                ["Quantity", number(selected.quantity, 5)],
                ["Entry price", money(selected.entry_price)],
                ["Position value", money(selected.position_size_usd)],
                ["Risk amount", money(selected.risk_amount_usd)],
                ["Stop boundary", money(selected.sl_price)],
                ["Target boundary", money(selected.tp_price)],
                ["P&L", money(selected.pnl)],
                ["Record ID", selected.trade_id],
              ].map(([k, v]) => (
                <div key={k}>
                  <span>{k}</span>
                  <strong className="mono">{v}</strong>
                </div>
              ))}
            </div>
            <div className="notice-box">
              <LockKeyhole size={16} />
              <span>
                Closing or modifying real positions is intentionally unavailable
                from this read-only console.
              </span>
            </div>
          </section>
        </div>
      )}
      {modal && (
        <div className="modal-backdrop" onClick={() => setModal(null)}>
          <section
            className="dialog"
            role="dialog"
            aria-modal="true"
            aria-label={
              modal === "live"
                ? "Live execution locked"
                : modal === "connection"
                  ? "Connection requirements"
                  : "Workspace information"
            }
            onClick={(e) => e.stopPropagation()}
          >
            <div className="dialog-top">
              <span className="small-cap">OPERATOR WORKSPACE</span>
              <button
                className="icon-button"
                aria-label="Close dialog"
                onClick={() => setModal(null)}
              >
                <X size={19} />
              </button>
            </div>
            <div className="dialog-symbol">
              <Icon
                as={
                  modal === "live"
                    ? ShieldCheck
                    : modal === "connection"
                      ? Globe
                      : modal === "launch"
                        ? FlaskConical
                        : modal === "notifications"
                          ? Bell
                          : BookOpen
                }
                size={26}
              />
            </div>
            <h2>
              {modal === "live"
                ? "Live is earned. Not toggled."
                : modal === "connection"
                  ? "Connect deliberately."
                  : modal === "launch"
                    ? "Prepare before you execute."
                    : modal === "notifications"
                      ? "A quiet, accountable workspace."
                      : "A clear operating model."}
            </h2>
            <p className="body-copy">
              {modal === "live"
                ? "This console cannot activate live trading. Readiness requires independent evidence, approved credentials and explicit human authorization."
                : modal === "connection"
                  ? `${exchange} is selected for workspace context only. No account has been connected and no credential has been stored. The native credential vault and account-activation control plane are not implemented yet.`
                  : modal === "launch"
                    ? "Create an unapplied paper/research configuration draft. Job launch, scheduling and backtest result retrieval are not connected in this frontend release."
                    : modal === "notifications"
                      ? "No runtime alert subscription is attached. A lack of alerts is not evidence of a healthy trading system."
                      : "Sample workspace uses clearly labelled illustrative financial data. Runtime view reads the existing backend telemetry and durable history adapters without exposing trading mutations."}
            </p>
            <div className="dialog-list">
              {(modal === "live"
                ? [
                    "Exchange and protective-intent recovery validation",
                    "Real-service persistence and account reconciliation",
                    "Paper soak, measured latency and independent review",
                    "Explicit session approval — never a browser toggle",
                  ]
                : modal === "connection"
                  ? [
                      "Read-only console API requires a long bearer token",
                      "Tokens stay in the local proxy environment, not browser storage",
                      "No exchange API keys are requested by this UI",
                    ]
                  : modal === "launch"
                    ? [
                        "Finite, bounded configuration validation",
                        "Local draft save and JSON export",
                        "No runtime launch or strategy changes",
                      ]
                    : [
                        "Inspect provenance before interpreting a metric",
                        "No samples means not measured, never zero",
                        "History is queried from the existing ClickHouse ledger",
                      ]
              ).map((t, i) => (
                <div key={t}>
                  <span className="mono">0{i + 1}</span>
                  {t}
                </div>
              ))}
            </div>
            <button
              className="button primary full"
              onClick={() => {
                setModal(null);
                if (modal === "launch") navigate("settings");
                else if (modal === "connection") navigate("exchanges");
              }}
            >
              {modal === "launch" ? "Open draft configuration" : "Understood"}
              <ArrowRight size={15} />
            </button>
          </section>
        </div>
      )}
    </div>
  );
}
