import { parseProjection } from "./projectionClient";
import type { Position, Snapshot, Stage } from "./data";
import type { Readiness } from "./ReadinessPanel";
const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const finite = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);
const count = (value: unknown) =>
  finite(value) && Number.isInteger(value) && value >= 0;
const text = (value: unknown): value is string =>
  typeof value === "string" && value.length > 0;
export function isPosition(value: unknown): value is Position {
  if (!object(value)) return false;
  return (
    text(value.trade_id) &&
    text(value.symbol) &&
    ["LONG", "SHORT"].includes(String(value.side)) &&
    text(value.state) &&
    text(value.entry_time) &&
    ["quantity", "entry_price", "position_size_usd", "risk_amount_usd"].every(
      (k) => finite(value[k]) && (value[k] as number) >= 0,
    ) &&
    (value.pnl === null || finite(value.pnl)) &&
    ["exit_price", "sl_price", "tp_price"].every(
      (k) => value[k] == null || finite(value[k]),
    )
  );
}
const isStage = (value: unknown): value is Stage =>
  object(value) &&
  text(value.stage) &&
  ["latency_p50_ms", "latency_p95_ms", "latency_p99_ms"].every(
    (k) => value[k] === null || (finite(value[k]) && (value[k] as number) >= 0),
  ) &&
  ["latency_sample_count", "input_count", "queue_depth", "error_count"].every(
    (k) => count(value[k]),
  );
function isReadiness(value: unknown): value is Readiness {
  if (
    !object(value) ||
    value.schema_version !== 1 ||
    value.live_authorized !== false ||
    !text(value.engine) ||
    !text(value.scope) ||
    !text(value.generated_at) ||
    !text(value.disclaimer) ||
    !text(value.verdict) ||
    !object(value.counts) ||
    !Array.isArray(value.gates) ||
    !Array.isArray(value.next_actions)
  )
    return false;
  if (
    !["passed", "blocked", "not_measured", "total"].every((k) =>
      count((value.counts as Record<string, unknown>)[k]),
    )
  )
    return false;
  const gates = value.gates;
  if (
    !gates.every(
      (g) =>
        object(g) &&
        ["id", "title", "evidence", "next_action", "priority"].every((k) =>
          text(g[k]),
        ) &&
        ["passed", "blocked", "not_measured"].includes(String(g.status)),
    )
  )
    return false;
  const ids = gates.map((g) => g.id);
  if (new Set(ids).size !== ids.length) return false;
  if (
    !value.next_actions.every(
      (a) =>
        object(a) &&
        text(a.gate_id) &&
        ids.includes(a.gate_id) &&
        text(a.priority) &&
        text(a.action),
    )
  )
    return false;
  return (
    value.counts.total === gates.length &&
    ["passed", "blocked", "not_measured"].every(
      (status) =>
        value.counts &&
        (value.counts as Record<string, unknown>)[status] ===
          gates.filter((g) => g.status === status).length,
    )
  );
}
export function parseSnapshot(value: unknown, now = Date.now()): Snapshot {
  if (
    !object(value) ||
    value.schema_version !== 1 ||
    typeof value.connected !== "boolean" ||
    !text(value.source) ||
    !text(value.mode) ||
    !text(value.generated_at) ||
    value.controls !== "read_only" ||
    typeof value.history_available !== "boolean" ||
    !object(value.configuration) ||
    !Array.isArray(value.positions) ||
    value.positions.length > 10000 ||
    !value.positions.every(isPosition) ||
    !Array.isArray(value.pipeline) ||
    !value.pipeline.every(isStage) ||
    !Array.isArray(value.modules) ||
    !value.modules.every((m) => object(m) && text(m.name) && text(m.status))
  )
    throw Error("Invalid console snapshot");
  const time = Date.parse(value.generated_at);
  const age = now - time;
  if (
    !/(Z|[+-]\d{2}:\d{2})$/.test(value.generated_at) ||
    !Number.isFinite(time) ||
    age > 15000 ||
    age < -5000
  )
    throw Error("Stale or invalid snapshot timestamp");
  // Optional new evidence schema may be unavailable on older backends. Never trust malformed gates.
  return {
    ...value,
    readiness: isReadiness(value.readiness) ? value.readiness : undefined,
    projection: parseProjection(value.projection, now) ?? undefined,
  } as Snapshot;
}
export function parseHistory(value: unknown): Position[] {
  if (
    !object(value) ||
    value.available !== true ||
    value.source !== "clickhouse" ||
    !Array.isArray(value.rows) ||
    value.rows.length > 100 ||
    !value.rows.every(isPosition)
  )
    throw Error("Invalid console history");
  return value.rows;
}
