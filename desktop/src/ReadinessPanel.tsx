import {
  ArrowDownToLine,
  ArrowRight,
  Check,
  LockKeyhole,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
import type { Snapshot } from "./data";
import { download } from "./data";
export type ReadinessGate = {
  id: string;
  title: string;
  status: "passed" | "blocked" | "not_measured";
  evidence: string;
  next_action: string;
  priority: string;
};
export type Readiness = {
  schema_version: 1;
  engine: string;
  scope: string;
  generated_at: string;
  live_authorized: false;
  verdict: string;
  counts: {
    passed: number;
    blocked: number;
    not_measured: number;
    total: number;
  };
  gates: ReadinessGate[];
  next_actions: { gate_id: string; priority: string; action: string }[];
  disclaimer: string;
};
export default function ReadinessPanel({
  snapshot,
}: {
  snapshot: Snapshot | null;
}) {
  const report = snapshot?.readiness;
  if (!report)
    return (
      <section className="panel padded">
        <div className="inline">
          <ShieldCheck size={20} />
          <h2>Readiness evidence unavailable</h2>
        </div>
        <p className="body-copy">
          The read-only evidence engine is not reachable or this backend does
          not provide its schema. No sample readiness certificate is
          substituted. Live remains locked.
        </p>
      </section>
    );
  const exportReport = () =>
    download(
      new Blob([JSON.stringify(report, null, 2)], { type: "application/json" }),
      "aitos-readiness-observations.json",
    );
  return (
    <>
      <section className="panel quality-hero">
        <div>
          <div className="eyebrow">
            <ShieldCheck size={16} /> EVIDENCE BEFORE CONFIDENCE
          </div>
          <h2>
            Trust is a trail.
            <br />
            Not a score.
          </h2>
          <p>
            Current bridge observations, explicit unknowns and the next safe
            action. This panel always uses the actual adapter, even in Sample
            workspace.
          </p>
          <div className="inline">
            <span className="pill amber">
              <LockKeyhole size={11} /> Live not authorized
            </span>
            <span className="quality-source mono">
              Deterministic rules · no LLM trading authority
            </span>
          </div>
        </div>
        <div className="quality-counts">
          {(
            [
              ["passed", "Observed checks"],
              ["blocked", "Blockers"],
              ["not_measured", "Not measured"],
            ] as const
          ).map(([key, label]) => (
            <div key={key}>
              <strong
                className={
                  key === "passed"
                    ? "positive"
                    : key === "blocked"
                      ? "negative"
                      : ""
                }
              >
                {report.counts[key]}
              </strong>
              <span>{label}</span>
            </div>
          ))}
        </div>
      </section>
      <div className="section-toolbar">
        <span className="dim">
          Counts are evidence categories, not a performance grade or production
          certification.
        </span>
        <button className="button secondary" onClick={exportReport}>
          <ArrowDownToLine size={14} />
          Export evidence
        </button>
      </div>
      <div className="quality-layout">
        <section className="panel">
          <div className="panel-heading">
            <h2>Operational evidence map</h2>
            <span className="small-cap">{report.gates.length} CHECKS</span>
          </div>
          <div className="quality-gates">
            {report.gates.map((g) => (
              <details className={`quality-gate ${g.status}`} key={g.id}>
                <summary>
                  <span className="quality-gate-icon">
                    {g.status === "passed" ? (
                      <Check size={15} />
                    ) : g.status === "blocked" ? (
                      <TriangleAlert size={15} />
                    ) : (
                      <ShieldCheck size={15} />
                    )}
                  </span>
                  <span>{g.title}</span>
                  <span
                    className={`pill ${g.status === "passed" ? "green" : g.status === "blocked" ? "amber" : ""}`}
                  >
                    {g.status.replaceAll("_", " ")}
                  </span>
                </summary>
                <div className="quality-evidence">
                  <span className="small-cap">OBSERVATION</span>
                  <p>{g.evidence}</p>
                  <span className="small-cap">NEXT SAFE ACTION</span>
                  <p>{g.next_action}</p>
                </div>
              </details>
            ))}
          </div>
        </section>
        <section className="panel padded quality-next">
          <div className="inline">
            <ArrowRight size={17} />
            <h2>Recommended next steps</h2>
          </div>
          <p className="body-copy">
            Blockers first. No recommendation executes automatically or
            overrides recovery ownership.
          </p>
          {report.next_actions.slice(0, 6).map((action, i) => (
            <div className="quality-action" key={action.gate_id}>
              <span className="mono">{String(i + 1).padStart(2, "0")}</span>
              <div>
                <span className="pill">{action.priority}</span>
                <p>{action.action}</p>
              </div>
            </div>
          ))}
          <div className="notice-box">
            <LockKeyhole size={16} />
            <span>{report.disclaimer}</span>
          </div>
        </section>
      </div>
      <div className="table-note quality-provenance">
        <ShieldCheck size={14} />
        <span>
          {report.engine} · Generated{" "}
          {new Date(report.generated_at).toLocaleString("en-GB", {
            timeZone: "UTC",
          })}{" "}
          UTC · {snapshot?.source}
        </span>
      </div>
    </>
  );
}
