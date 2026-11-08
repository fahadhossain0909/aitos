import { consoleFetch } from "./operatorSession";
import { useEffect, useRef, useState } from "react";
import {
  ArrowDownToLine,
  ArrowRight,
  CalendarDays,
  FlaskConical,
  Info,
  LockKeyhole,
} from "lucide-react";
import { download } from "./data";
type Fold = {
  index: number;
  train_start: string;
  train_end: string;
  gap_start: string;
  test_start: string;
  test_end: string;
};
type Plan = {
  schema_version: 1;
  engine: string;
  scope: "partition_plan_only";
  evidence_sha256: string;
  gap_days: number;
  fold_count: number;
  folds: Fold[];
  holdout: { start: string; end: string };
  holdout_fully_available: boolean;
  status: string;
  dataset_evidence: string;
  backtest_executed: false;
  promotion_authorized: false;
  limitations: string[];
};
const isoDate = (v: unknown): v is string =>
  typeof v === "string" &&
  /^\d{4}-\d{2}-\d{2}$/.test(v) &&
  Number.isFinite(Date.parse(v)) &&
  new Date(v).toISOString().slice(0, 10) === v;
export function parsePlan(value: unknown): Plan {
  const p = value as Partial<Plan> | null;
  if (
    !p ||
    p.schema_version !== 1 ||
    p.scope !== "partition_plan_only" ||
    p.backtest_executed !== false ||
    p.promotion_authorized !== false ||
    typeof p.engine !== "string" ||
    typeof p.status !== "string" ||
    typeof p.dataset_evidence !== "string" ||
    typeof p.evidence_sha256 !== "string" ||
    !/^[a-f0-9]{64}$/.test(p.evidence_sha256) ||
    !Number.isInteger(p.gap_days) ||
    (p.gap_days as number) < 0 ||
    (p.gap_days as number) > 60 ||
    !["planned_not_validated", "insufficient_history"].includes(p.status) ||
    !["not_supplied", "provided_not_verified"].includes(p.dataset_evidence) ||
    !Number.isInteger(p.fold_count) ||
    !Array.isArray(p.folds) ||
    p.folds.length > 200 ||
    p.fold_count !== p.folds.length ||
    !p.folds.every(
      (f, i) =>
        f &&
        f.index === i + 1 &&
        [
          "train_start",
          "train_end",
          "gap_start",
          "test_start",
          "test_end",
        ].every((k) => isoDate(f[k as keyof Fold])) &&
        f.train_start < f.train_end &&
        f.train_end <= f.test_start &&
        f.test_start < f.test_end,
    ) ||
    !p.holdout ||
    !isoDate(p.holdout.start) ||
    !isoDate(p.holdout.end) ||
    typeof p.holdout_fully_available !== "boolean" ||
    !Array.isArray(p.limitations) ||
    !p.limitations.every((s) => typeof s === "string")
  )
    throw Error("Invalid research partition plan");
  const gap = p.gap_days as number;
  if (
    p.holdout.start >= p.holdout.end ||
    p.folds.length > 0 !== (p.status === "planned_not_validated")
  )
    throw Error("Invalid plan status or holdout");
  for (let i = 0; i < p.folds.length; i++) {
    const f = p.folds[i];
    if (
      f.gap_start !== f.train_end ||
      Date.parse(f.test_start) - Date.parse(f.train_end) !== gap * 86400000 ||
      Date.parse(f.test_end) > Date.parse(p.holdout.start) - gap * 86400000 ||
      (i > 0 && f.test_start < p.folds[i - 1].test_end)
    )
      throw Error("Invalid partition gap or holdout separation");
  }
  return p as Plan;
}
export default function ResearchPlanner() {
  const [inputs, setInputs] = useState({
    start: "2025-01-01",
    end: new Date().toISOString().slice(0, 10),
    train_days: 180,
    test_days: 30,
    holdout_days: 30,
    label_horizon_days: 1,
    embargo_days: 1,
    dataset_sha256: "",
  });
  const [plan, setPlan] = useState<Plan | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null),
    requestId = useRef(0);
  useEffect(
    () => () => {
      requestId.current++;
      controller.current?.abort();
    },
    [],
  );
  const change = (key: keyof typeof inputs, value: string | number) => {
    requestId.current++;
    controller.current?.abort();
    setBusy(false);
    setPlan(null);
    setError("");
    setInputs((old) => ({ ...old, [key]: value }));
  };
  async function build() {
    const id = ++requestId.current;
    controller.current?.abort();
    const c = new AbortController();
    controller.current = c;
    const timeout = setTimeout(() => c.abort(), 8000);
    setBusy(true);
    setError("");
    setPlan(null);
    try {
      const params = new URLSearchParams(
        Object.entries(inputs)
          .filter(([, v]) => v !== "")
          .map(([k, v]) => [k, String(v)]),
      );
      const r = await consoleFetch(`/console/v1/research/plan?${params}`, {
        signal: c.signal,
        cache: "no-store",
      });
      if (!r.ok)
        throw Error(
          "The design is outside supported date, gap or fold-count bounds.",
        );
      const report = parsePlan(await r.json());
      if (id === requestId.current) setPlan(report);
    } catch (e) {
      if (id === requestId.current)
        setError(
          c.signal.aborted
            ? "Planning request timed out. No result was retained."
            : e instanceof Error
              ? e.message
              : "Research planner unavailable.",
        );
    } finally {
      clearTimeout(timeout);
      if (id === requestId.current) setBusy(false);
    }
  }
  return (
    <section className="research-planner">
      <div className="planner-layout">
        <section className="panel padded">
          <div className="inline">
            <CalendarDays size={18} />
            <h2>Design the unseen windows</h2>
          </div>
          <p className="body-copy">
            A deterministic date plan. No market data is loaded, no strategy is
            run and no holdout is inspected.
          </p>
          <div className="planner-date-grid">
            {(["start", "end"] as const).map((key) => (
              <label key={key}>
                <span>
                  {key === "start"
                    ? "History start (UTC)"
                    : "History end (exclusive, UTC)"}
                </span>
                <input
                  type="date"
                  value={inputs[key]}
                  onChange={(e) => change(key, e.target.value)}
                />
              </label>
            ))}
          </div>
          {(
            [
              {
                key: "train_days",
                label: "Rolling training window",
                min: 1,
                max: 3650,
              },
              {
                key: "test_days",
                label: "Unseen test window",
                min: 1,
                max: 365,
              },
              {
                key: "holdout_days",
                label: "Locked holdout",
                min: 1,
                max: 365,
              },
              {
                key: "label_horizon_days",
                label: "Maximum label horizon",
                min: 0,
                max: 30,
              },
              {
                key: "embargo_days",
                label: "Additional pre-test buffer",
                min: 0,
                max: 30,
              },
            ] as const
          ).map((f) => (
            <label className="config-field" key={f.key}>
              <div>
                <strong>{f.label}</strong>
                <small>
                  Calendar days · {f.min}–{f.max}
                </small>
              </div>
              <div className="number-input">
                <input
                  type="number"
                  aria-label={f.label}
                  value={inputs[f.key]}
                  min={f.min}
                  max={f.max}
                  step="1"
                  onChange={(e) => change(f.key, Number(e.target.value))}
                />
                <span>days</span>
              </div>
            </label>
          ))}
          <label className="planner-fingerprint">
            <span>Optional dataset SHA-256 · provided, not verified</span>
            <input
              aria-label="Dataset SHA256"
              value={inputs.dataset_sha256}
              maxLength={64}
              placeholder="64 lowercase hexadecimal characters"
              onChange={(e) => change("dataset_sha256", e.target.value.trim())}
            />
          </label>
          {error && (
            <div className="validation-error" role="alert">
              {error}
            </div>
          )}
          <button
            className="button primary full"
            disabled={busy}
            onClick={build}
          >
            <FlaskConical size={15} />
            {busy ? "Building bounded plan…" : "Build evaluation plan"}
            <ArrowRight size={15} />
          </button>
        </section>
        <section className="panel padded planner-principles">
          <span className="small-cap">RESEARCH DISCIPLINE</span>
          <h2>
            An unseen test must
            <br />
            stay unseen.
          </h2>
          <p className="body-copy">
            Reserve training, a conservative temporal gap, chronological test
            windows and a final locked holdout before inspecting outcomes.
          </p>
          <div className="planner-process">
            <span>TRAIN</span>
            <i />
            <span>GAP</span>
            <i />
            <span>TEST</span>
            <i />
            <span>LOCK</span>
          </div>
          <div className="checks">
            <span>
              <LockKeyhole size={14} />
              No automatic strategy promotion
            </span>
            <span>
              <LockKeyhole size={14} />
              No dataset verification inferred from a hash
            </span>
            <span>
              <LockKeyhole size={14} />
              No profitability claim from a partition plan
            </span>
          </div>
          <p className="body-copy">
            The pre-test gap equals declared label horizon plus additional
            buffer. Actual event-label intervals, fees, slippage, trial counts
            and candidate freezing still need independent validation.
          </p>
          <div className="notice-box">
            <Info size={16} />
            <span>
              This planner does not alter the existing trading or walk-forward
              policy.
            </span>
          </div>
        </section>
      </div>
      {plan && (
        <section className="panel planner-results">
          <div className="panel-heading">
            <div className="inline">
              <h2>
                {plan.fold_count
                  ? `${plan.fold_count} chronological folds`
                  : "Insufficient history for a complete test fold"}
              </h2>
              <span className="pill amber">Plan only · not validated</span>
            </div>
            <button
              className="text-button"
              onClick={() =>
                download(
                  new Blob([JSON.stringify(plan, null, 2)], {
                    type: "application/json",
                  }),
                  "aitos-research-partition-plan.json",
                )
              }
            >
              <ArrowDownToLine size={14} />
              Export plan
            </button>
          </div>
          <div className="planner-holdout">
            <LockKeyhole size={17} />
            <div>
              <strong>
                Locked holdout · {plan.holdout.start} → {plan.holdout.end} UTC
              </strong>
              <p>
                {plan.gap_days} days reserved before tests and holdout ·{" "}
                {plan.holdout_fully_available
                  ? "Full holdout duration available in date range"
                  : "Requested holdout exceeds date range"}{" "}
                · Dataset evidence: {plan.dataset_evidence.replaceAll("_", " ")}
              </p>
            </div>
          </div>
          {plan.folds.length > 0 && (
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>Fold</th>
                    <th>Training [start, end)</th>
                    <th>Temporal gap</th>
                    <th>Unseen test [start, end)</th>
                  </tr>
                </thead>
                <tbody>
                  {plan.folds.map((f) => (
                    <tr key={f.index}>
                      <td className="mono">
                        {String(f.index).padStart(2, "0")}
                      </td>
                      <td className="mono">
                        {f.train_start} → {f.train_end}
                      </td>
                      <td>
                        <span className="pill">{plan.gap_days} days</span>
                      </td>
                      <td className="mono positive">
                        {f.test_start} → {f.test_end}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <div className="padded">
            <details>
              <summary>Evidence, assumptions and limitations</summary>
              <pre>
                {JSON.stringify(
                  {
                    sha256: plan.evidence_sha256,
                    engine: plan.engine,
                    scope: plan.scope,
                    limitations: plan.limitations,
                  },
                  null,
                  2,
                )}
              </pre>
            </details>
          </div>
        </section>
      )}
    </section>
  );
}
