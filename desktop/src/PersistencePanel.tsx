import { useState } from "react";
import {
  ArrowDownToLine,
  ArrowRight,
  Check,
  Database,
  FlaskConical,
  Layers3,
  LockKeyhole,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
import type { Snapshot } from "./data";
import { download } from "./data";
import { parseProjection } from "./projectionClient";

export default function PersistencePanel({
  snapshot,
}: {
  snapshot: Snapshot | null;
}) {
  const [illustrate, setIllustrate] = useState(false);
  const observed = parseProjection(snapshot?.projection);
  const report = illustrate
    ? {
        available: true,
        status: "verified",
        source: "illustrative",
        observed_at: null,
        counts: { total: 12, pending: 3, delivered: 9 },
        last_batch: { checked: 4, inserted: 3, acknowledged: 4 },
      }
    : observed;
  const available = report?.available === true;
  const exportObservation = () => {
    if (!available) return;
    download(
      new Blob(
        [
          JSON.stringify(
            {
              schema_version: 1,
              kind: "projection_console_export",
              synthetic: illustrate,
              live_authorized: false,
              production_certified: false,
              observation: report,
            },
            null,
            2,
          ),
        ],
        { type: "application/json" },
      ),
      "aitos-persistence-observation.json",
    );
  };
  const status = observed?.status;
  return (
    <div className="persistence-workspace">
      <section className="panel persistence-hero">
        <div className="eyebrow">
          <Layers3 size={16} /> THE PATH FROM COMMIT TO EVIDENCE
        </div>
        <h2>
          Persist with intent.
          <br />
          <span>Verify before acknowledgement.</span>
        </h2>
        <p>
          Immutable local history and a bounded evidence projection. A delivered
          row is a local acknowledgement—not proof of account reconciliation,
          continuous sink integrity or permission to trade.
        </p>
        <div className="persistence-toolbar">
          <span className="pill amber">
            <LockKeyhole size={12} /> Read-only · live locked
          </span>
          <button
            className="button secondary"
            onClick={() => setIllustrate(!illustrate)}
          >
            <FlaskConical size={14} />
            {illustrate
              ? "Return to observed evidence"
              : "Explore synthetic example"}
          </button>
          <button
            className="button secondary"
            disabled={!available}
            onClick={exportObservation}
          >
            <ArrowDownToLine size={14} />
            Export observation
          </button>
        </div>
      </section>
      {illustrate ? (
        <div className="persistence-notice synthetic" role="status">
          <FlaskConical size={17} />
          <div>
            <strong>ILLUSTRATIVE WORKFLOW — NOT RUNTIME EVIDENCE</strong>
            <p>
              These counts explain retry behavior. Nothing is connected,
              projected or executed by this example. Exports remain explicitly
              synthetic.
            </p>
          </div>
        </div>
      ) : !available ? (
        <div className="persistence-notice" role="status">
          <TriangleAlert size={18} />
          <div>
            <h3>Persistence evidence unavailable</h3>
            <p>
              {status && status !== "unavailable"
                ? `Last observation: ${status}. No counters are retained after an unsuccessful or unconfirmed attempt.`
                : "No fresh projector observation is attached. Unavailable is not zero pending work."}{" "}
              No sample certificate is substituted, even in Sample workspace.
            </p>
          </div>
        </div>
      ) : (
        <div className="persistence-notice observed" role="status">
          <ShieldCheck size={17} />
          <div>
            <strong>
              {report?.status === "empty"
                ? "No pending batch at last observation"
                : "Last bounded batch verified"}
            </strong>
            <p>
              Observed{" "}
              {report?.observed_at
                ? new Date(report.observed_at).toLocaleString()
                : "—"}{" "}
              · local journal only · expires after 60 seconds without new
              evidence.
            </p>
          </div>
        </div>
      )}
      <div className="persistence-metrics" aria-label="Projection counters">
        {[
          [
            "Pending evidence",
            report?.counts?.pending,
            "Awaiting verified sink acceptance",
          ],
          [
            "Locally acknowledged",
            report?.counts?.delivered,
            "Retained receipts; not a sink certificate",
          ],
          [
            "Retained outbox records",
            report?.counts?.total,
            "Pending + locally acknowledged",
          ],
        ].map(([label, value, detail]) => (
          <section className="panel persistence-metric" key={String(label)}>
            <span>{label}</span>
            <strong>
              {available && typeof value === "number"
                ? value.toLocaleString()
                : "—"}
            </strong>
            <small>{detail}</small>
          </section>
        ))}
      </div>
      <section className="panel padded">
        <div className="section-heading">
          <div>
            <div className="eyebrow">ONE BATCH, THREE CHECKPOINTS</div>
            <h3>Delivery without blind retries.</h3>
          </div>
          <span className="pill">At least once</span>
        </div>
        <div className="persistence-flow">
          {[
            [
              "01",
              "Read committed events",
              "Validate scoped history, receipt uniqueness and current storage writer.",
              report?.last_batch?.checked,
            ],
            [
              "02",
              "Inspect, then insert",
              "Check existing sink keys. Insert only missing immutable evidence.",
              report?.last_batch?.inserted,
            ],
            [
              "03",
              "Verify, then acknowledge",
              "Read back the complete batch before one atomic local acknowledgement.",
              report?.last_batch?.acknowledged,
            ],
          ].map(([index, title, description, value], i) => (
            <div className="persistence-step" key={String(index)}>
              <span className="mono persistence-step-id">{index}</span>
              <h4>{title}</h4>
              <p>{description}</p>
              <div>
                <Check size={13} />
                <strong>
                  {available && typeof value === "number" ? value : "—"}
                </strong>
                <span>events in last batch</span>
              </div>
              {i < 2 && (
                <ArrowRight className="persistence-flow-arrow" size={18} />
              )}
            </div>
          ))}
        </div>
      </section>
      <div className="persistence-limits">
        <section className="panel padded">
          <Database size={20} />
          <h3>A timeout is not a negative receipt.</h3>
          <p>
            Sink writes can succeed before a response is lost. The next explicit
            batch inspects existing evidence before retrying. Conflicts leave
            local events pending; raw upstream errors are not exposed.
          </p>
        </section>
        <section className="panel padded">
          <LockKeyhole size={20} />
          <h3>Evidence is not execution authority.</h3>
          <p>
            No start-projector, account mutation, credential, order or migration
            buttons are provided here. Storage fencing does not cancel in-flight
            venue requests. Production review and real-service soak remain
            separate gates.
          </p>
        </section>
      </div>
    </div>
  );
}
