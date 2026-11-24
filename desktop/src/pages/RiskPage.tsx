import { ArrowRight, ShieldCheck } from "lucide-react";
import type { Position } from "../data";
import { money } from "../data";
import { Empty, Pill } from "../components/common";
import {
  DEMO_CONFIGURATION,
  DEMO_EQUITY,
  formatUsed,
  readLimits,
  summarizeRisk,
  type LimitRow,
  type LimitStatus,
} from "../riskModel";

const STATUS_LABEL: Record<LimitStatus, string> = {
  ok: "Within limit",
  watch: "Near or at limit",
  breach: "Over limit",
  unmeasured: "Not measured",
};
const STATUS_TONE: Record<LimitStatus, string> = {
  ok: "green",
  watch: "amber",
  breach: "red",
  unmeasured: "muted",
};

function limitText(row: LimitRow) {
  if (row.limit === null) return "No limit configured";
  return `limit ${formatUsed(row.limit, row.unit)}`;
}

export default function RiskPage({
  positions,
  configuration,
  demo,
  onOpenSettings,
}: {
  positions: Position[];
  configuration: Record<string, unknown>;
  demo: boolean;
  onOpenSettings: () => void;
}) {
  const summary = summarizeRisk(
    positions,
    demo ? DEMO_EQUITY : null,
    readLimits(demo ? DEMO_CONFIGURATION : configuration),
  );
  const measured = summary.rows.filter((r) => r.status !== "unmeasured");
  const breaches = measured.filter((r) => r.status === "breach").length;
  const watches = measured.filter((r) => r.status === "watch").length;
  return (
    <>
      <div className="notice-box risk-notice" role="note">
        <ShieldCheck size={16} />
        <span>
          {demo
            ? "Illustrative positions and an illustrative account value. Nothing here is a live measurement."
            : "Derived only from open positions and the read-only configuration. Without an attached account value, percentage limits are shown as not measured instead of being estimated."}
        </span>
      </div>
      <div className="stats-grid">
        <div className="stat">
          <div className="stat-label">Gross exposure</div>
          <div className="stat-value">
            {money(summary.gross)}
            <span className="currency">USD</span>
          </div>
          <div className="stat-foot">
            <Pill>{summary.count} open</Pill>
            <span>
              {summary.equity
                ? `${(summary.gross / summary.equity).toFixed(2)}× account value`
                : "Account value not attached"}
            </span>
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Net exposure</div>
          <div className="stat-value">
            {money(summary.net)}
            <span className="currency">USD</span>
          </div>
          <div className="stat-foot">
            <Pill>Long {money(summary.longUsd, 0)}</Pill>
            <Pill>Short {money(summary.shortUsd, 0)}</Pill>
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Risk at stop</div>
          <div className="stat-value">
            {money(summary.riskAtStop)}
            <span className="currency">USD</span>
          </div>
          <div className="stat-foot">
            <span>Sum of planned stop-loss risk</span>
          </div>
        </div>
        <div className="stat">
          <div className="stat-label">Unrealized P&amp;L</div>
          <div className={`stat-value ${summary.unrealizedPnl >= 0 ? "positive" : "negative"}`}>
            {summary.unrealizedPnl >= 0 ? "+" : "−"}
            {money(Math.abs(summary.unrealizedPnl))}
          </div>
          <div className="stat-foot">
            <span>
              {summary.unknownPnl
                ? `${summary.unknownPnl} position(s) without a mark price`
                : "All positions marked"}
            </span>
          </div>
        </div>
      </div>
      <div className="risk-layout">
        <section className="panel">
          <div className="panel-heading">
            <div className="inline">
              <h2>Limit usage</h2>
              <Pill tone={breaches ? "red" : watches ? "amber" : "green"}>
                {breaches
                  ? `${breaches} over limit`
                  : watches
                    ? `${watches} near limit`
                    : measured.length
                      ? "Within limits"
                      : "Nothing measurable"}
              </Pill>
            </div>
            <button className="text-button" onClick={onOpenSettings}>
              Review parameters <ArrowRight size={13} />
            </button>
          </div>
          <ul className="risk-limits">
            {summary.rows.map((row) => {
              const ratio =
                row.used !== null && row.limit ? Math.min(100, (row.used / row.limit) * 100) : 0;
              return (
                <li key={row.id} className={`risk-limit ${row.status}`}>
                  <div className="risk-limit-head">
                    <span>{row.label}</span>
                    <Pill tone={STATUS_TONE[row.status]}>{STATUS_LABEL[row.status]}</Pill>
                  </div>
                  <div className="risk-limit-value">
                    <strong>{formatUsed(row.used, row.unit)}</strong>
                    <small>{limitText(row)}</small>
                  </div>
                  <div
                    className={`risk-track ${row.status}`}
                    role="progressbar"
                    aria-label={row.label}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    aria-valuenow={Math.round(ratio)}
                  >
                    <span style={{ width: `${ratio}%` }} />
                  </div>
                  <p>{row.note}</p>
                </li>
              );
            })}
          </ul>
        </section>
        <section className="panel">
          <div className="panel-heading">
            <div className="inline">
              <h2>Exposure by instrument</h2>
              <span className="count-box">{summary.symbols.length}</span>
            </div>
          </div>
          {summary.symbols.length === 0 ? (
            <Empty
              title="No open positions"
              description="Exposure appears here when the attached runtime reports open trades."
            />
          ) : (
            <div className="risk-table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Instrument</th>
                    <th>Open</th>
                    <th>Notional</th>
                    <th>Share of gross</th>
                    <th>Risk at stop</th>
                    <th>Unrealized P&amp;L</th>
                  </tr>
                </thead>
                <tbody>
                  {summary.symbols.map((s) => (
                    <tr key={s.symbol}>
                      <td>{s.symbol}</td>
                      <td>{s.count}</td>
                      <td>{money(s.notional)}</td>
                      <td>
                        <div className="risk-share">
                          <div className="risk-track neutral" aria-hidden="true">
                            <span style={{ width: `${(s.share * 100).toFixed(1)}%` }} />
                          </div>
                          <span>{(s.share * 100).toFixed(1)}%</span>
                        </div>
                      </td>
                      <td>{money(s.risk)}</td>
                      <td className={s.pnl >= 0 ? "positive" : "negative"}>
                        {s.pnl >= 0 ? "+" : "−"}
                        {money(Math.abs(s.pnl))}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </>
  );
}
