import { consoleFetch } from "./operatorSession";
import { useEffect, useState } from "react";
import {
  ArrowDownToLine,
  ArrowUpRight,
  BarChart3,
  Database,
  Info,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { download, money, number } from "./data";
import { parsePerformance } from "./performanceClient";
import type { PerformanceReport } from "./performanceClient";
function PnlChart({ report }: { report: PerformanceReport }) {
  const [selected, setSelected] = useState<number | null>(null);
  const rows = report.curve,
    index =
      selected === null ? rows.length - 1 : Math.min(selected, rows.length - 1),
    point = rows[index];
  const min = Math.min(0, ...rows.map((p) => p.net_pnl_usd)),
    max = Math.max(0, ...rows.map((p) => p.net_pnl_usd)),
    spread = Math.max(1, max - min);
  const first = Date.parse(rows[0].at),
    last = Date.parse(rows[rows.length - 1].at);
  const coords = rows.map((p) => [
    20 + ((Date.parse(p.at) - first) / Math.max(1, last - first)) * 840,
    30 + ((max - p.net_pnl_usd) / spread) * 190,
  ]);
  const path = coords.map(([x, y], i) => `${i ? "L" : "M"}${x},${y}`).join(" "),
    base = 30 + (max / spread) * 190;
  return (
    <section className="panel pnl-chart-panel">
      <div className="panel-heading">
        <div>
          <span className="small-cap">CUMULATIVE RECORDED NET P&L</span>
          <h2 className="performance-chart-value">
            {money(point.net_pnl_usd)}
          </h2>
        </div>
        <span className="pill">Final-close allocation · USD</span>
      </div>
      <div className="pnl-chart-wrap">
        <svg
          viewBox="0 0 880 260"
          role="img"
          aria-label="Cumulative recorded closed-trade net PnL, not account equity"
        >
          <defs>
            <linearGradient id="pnlFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="#c6ec9e" stopOpacity=".18" />
              <stop offset="1" stopColor="#c6ec9e" stopOpacity="0" />
            </linearGradient>
          </defs>
          {[30, 94, 157, 220].map((y) => (
            <line
              key={y}
              x1="20"
              x2="860"
              y1={y}
              y2={y}
              stroke="#36432d"
              strokeDasharray="3 6"
            />
          ))}
          <line
            x1="20"
            x2="860"
            y1={base}
            y2={base}
            stroke="#91a57f"
            strokeOpacity=".6"
          />
          <path
            d={`${path} L${coords[coords.length - 1][0]},${base} L20,${base} Z`}
            fill="url(#pnlFill)"
          />
          <path d={path} fill="none" stroke="#c6ec9e" strokeWidth="2.3" />
          <circle
            cx={coords[index][0]}
            cy={coords[index][1]}
            r="4"
            fill="#e2f5c8"
          />
          <text x="20" y="250" fill="#8ca07b" fontSize="10">
            {new Date(rows[0].at).toLocaleDateString("en-GB", {
              timeZone: "UTC",
            })}
          </text>
          <text x="860" y="250" fill="#8ca07b" textAnchor="end" fontSize="10">
            {new Date(rows[rows.length - 1].at).toLocaleDateString("en-GB", {
              timeZone: "UTC",
            })}
          </text>
        </svg>
      </div>
      <div className="performance-scrubber">
        <input
          type="range"
          min="0"
          max={rows.length - 1}
          value={index}
          onChange={(e) => setSelected(Number(e.target.value))}
          aria-label="Inspect closed-PnL observation"
        />
        <div>
          <span>
            {new Date(point.at).toLocaleString("en-GB", { timeZone: "UTC" })}{" "}
            UTC
          </span>
          <span>Closed-PnL drawdown {money(point.drawdown_usd)}</span>
        </div>
      </div>
      <div className="table-note">
        <Info size={13} />
        No account balance, open exposure or cash-flow timing is inferred.
      </div>
    </section>
  );
}
export default function PerformancePanel({
  bridgeFailed,
}: {
  bridgeFailed: boolean;
}) {
  const [report, setReport] = useState<PerformanceReport | null>(null),
    [loading, setLoading] = useState(true),
    [error, setError] = useState(false),
    [refresh, setRefresh] = useState(0);
  useEffect(() => {
    setReport(null);
    if (bridgeFailed) {
      setLoading(false);
      setError(true);
      return;
    }
    let disposed = false;
    const c = new AbortController(),
      timer = setTimeout(() => c.abort(), 8000);
    setLoading(true);
    setError(false);
    consoleFetch("/console/v1/performance", {
      signal: c.signal,
      cache: "no-store",
    })
      .then((r) => {
        if (!r.ok) throw Error();
        return r.json();
      })
      .then((d) => {
        if (!disposed) setReport(parsePerformance(d));
      })
      .catch(() => {
        if (!disposed) setError(true);
      })
      .finally(() => {
        clearTimeout(timer);
        if (!disposed) setLoading(false);
      });
    return () => {
      disposed = true;
      clearTimeout(timer);
      c.abort();
    };
  }, [bridgeFailed, refresh]);
  return (
    <>
      <section className="panel performance-hero">
        <div>
          <div className="eyebrow">
            <BarChart3 size={16} /> A LEDGER, NOT A PROMISE
          </div>
          <h2>
            Performance.
            <br />
            With provenance.
          </h2>
          <p>
            Actual persisted closed-trade evidence only, including in Sample
            workspace. A bounded ledger view—not an account-equity or
            strategy-promotion certificate.
          </p>
        </div>
        <div className="performance-hero-actions">
          <span className="pill">
            <Database size={12} />
            Read-only ClickHouse adapter
          </span>
          <button
            className="button secondary"
            disabled={loading || bridgeFailed}
            onClick={() => setRefresh((n) => n + 1)}
          >
            <RefreshCw size={14} />
            Refresh evidence
          </button>
          {report && (
            <button
              className="text-button"
              onClick={() =>
                download(
                  new Blob([JSON.stringify(report, null, 2)], {
                    type: "application/json",
                  }),
                  "aitos-recorded-performance.json",
                )
              }
            >
              <ArrowDownToLine size={14} />
              Export report
            </button>
          )}
        </div>
      </section>
      {loading ? (
        <section className="panel empty">
          <Database size={25} />
          <h3>Loading bounded ledger evidence</h3>
          <p>
            One query, up to 1,000 latest ledger updates. Earlier samples are
            not retained while loading.
          </p>
        </section>
      ) : error || !report ? (
        <section className="panel empty">
          <Database size={25} />
          <h3>Performance evidence unavailable</h3>
          <p>
            Attach the authenticated history adapter with valid, unambiguous
            closed records. No illustrative performance or zero account balance
            is substituted.
          </p>
        </section>
      ) : report.loaded_trades === 0 ? (
        <section className="panel empty">
          <Database size={25} />
          <h3>No persisted closed trades</h3>
          <p>
            The query succeeded with an empty ledger sample. Missing financial
            outcomes remain unavailable, not zero returns.
          </p>
        </section>
      ) : (
        <>
          <div className="section-toolbar">
            <span className="dim">
              {report.loaded_trades} latest-update ledger records ·{" "}
              {report.truncated
                ? "Window truncated; older records excluded"
                : "No extra record observed beyond this bounded window"}{" "}
              · ordered by close time for display
            </span>
            <span className="pill">Not all-time account performance</span>
          </div>
          <div className="stats-grid performance-stats">
            {[
              [
                "Recorded net P&L",
                money(report.summary.net_pnl_usd),
                "Costs are already reflected by the source record",
              ],
              [
                "Win rate",
                report.summary.win_rate === null
                  ? "—"
                  : `${number(report.summary.win_rate * 100)}%`,
                `${report.summary.wins} wins · ${report.summary.losses} losses · ${report.summary.breakeven} breakeven`,
              ],
              [
                "Expectancy",
                money(report.summary.expectancy_usd),
                "Arithmetic mean net P&L per closed record",
              ],
              [
                "Profit factor",
                number(report.summary.profit_factor, 2),
                report.summary.profit_factor === null
                  ? "Undefined without observed losses"
                  : "Positive P&L / absolute negative P&L",
              ],
            ].map(([title, value, note]) => (
              <div className="stat" key={title}>
                <div className="stat-label">
                  {title}
                  <ArrowUpRight size={14} />
                </div>
                <div className="stat-value">{value}</div>
                <div className="stat-foot">{note}</div>
              </div>
            ))}
          </div>
          <div className="performance-layout">
            <PnlChart report={report} />
            <section className="panel padded">
              <div className="inline">
                <ShieldCheck size={17} />
                <h2>The risk context</h2>
              </div>
              <div className="performance-risk-number">
                {money(report.summary.max_closed_pnl_drawdown_usd)}
              </div>
              <span className="small-cap">MAX CLOSED-PNL DRAWDOWN · USD</span>
              <p className="body-copy">
                Peak-to-trough decline in cumulative recorded P&L, starting from
                zero. Simultaneous final closes are grouped. This is not
                mark-to-market account drawdown or a percentage loss.
              </p>
              <div className="performance-fact">
                <span>Best closed record</span>
                <strong>{money(report.summary.best_trade_usd)}</strong>
              </div>
              <div className="performance-fact">
                <span>Worst closed record</span>
                <strong>{money(report.summary.worst_trade_usd)}</strong>
              </div>
              <div className="performance-fact">
                <span>Median closed P&L</span>
                <strong>{money(report.summary.median_pnl_usd)}</strong>
              </div>
            </section>
          </div>
          <div className="two-columns">
            <section className="panel padded">
              <h2>Direction is context</h2>
              {(["LONG", "SHORT"] as const).map((side) => (
                <div className="performance-direction" key={side}>
                  <div>
                    <span className="pill">{side}</span>
                    <small>
                      {report.directions[side].trades} closed records
                    </small>
                  </div>
                  <strong className="mono">
                    {money(report.directions[side].net_pnl_usd)}
                  </strong>
                </div>
              ))}
              <p className="body-copy">
                Unequal samples are descriptive, not proof that one direction
                has a durable edge.
              </p>
            </section>
            <section className="panel padded">
              <h2>Recorded cost coverage</h2>
              {(["fees_paid_usd", "funding_pnl_usd"] as const).map((key) => (
                <div className="performance-direction" key={key}>
                  <div>
                    <strong>
                      {key === "fees_paid_usd" ? "Fees paid" : "Funding P&L"}
                    </strong>
                    <small>
                      {report.costs[key].observed_rows} /{" "}
                      {report.costs[key].total_rows} records observed
                    </small>
                  </div>
                  <strong className="mono">
                    {money(report.costs[key].total_usd)}
                  </strong>
                </div>
              ))}
              <p className="body-copy">
                Missing cost fields stay unavailable. Recorded costs are not
                deducted again from net P&L.
              </p>
            </section>
          </div>
          <section className="panel performance-table">
            <div className="panel-heading">
              <h2>Frozen strategy attribution</h2>
              <span className="small-cap">DESCRIPTIVE ONLY</span>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Strategy identity</th>
                    <th>Closed records</th>
                    <th>Recorded net P&L</th>
                    <th>Expectancy</th>
                    <th>Profit factor</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(report.strategies).map(([id, s]) => (
                    <tr key={id}>
                      <td>{id}</td>
                      <td className="mono">{s.trades}</td>
                      <td className="mono">{money(s.net_pnl_usd)}</td>
                      <td className="mono">{money(s.expectancy_usd)}</td>
                      <td className="mono">{number(s.profit_factor, 2)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          <section className="panel padded performance-limitations">
            <h2>What this evidence does not say</h2>
            {report.limitations.map((text) => (
              <p key={text}>
                <Info size={13} />
                {text}
              </p>
            ))}
            <details>
              <summary>Evidence fingerprint</summary>
              <pre>
                {JSON.stringify(
                  {
                    sha256: report.evidence_sha256,
                    generated_at: report.generated_at,
                    scope: report.scope,
                    selection: report.selection,
                  },
                  null,
                  2,
                )}
              </pre>
            </details>
          </section>
        </>
      )}
    </>
  );
}
