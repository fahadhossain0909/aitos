export type ProjectionReport = {
  schema_version: 1;
  scope: "local_projection_observation";
  available: boolean;
  status:
    | "unavailable"
    | "running"
    | "empty"
    | "verified"
    | "conflict"
    | "fenced"
    | "timeout"
    | "failed"
    | "cancelled";
  reason: string;
  observed_at: string | null;
  counts: { total: number; pending: number; delivered: number } | null;
  last_batch: {
    checked: number;
    inserted: number;
    acknowledged: number;
  } | null;
  source: "local_journal" | "unavailable";
  controls: "read_only";
  live_authorized: false;
  production_certified: false;
};
const object = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === "object" && !Array.isArray(v);
const count = (v: unknown): v is number =>
  typeof v === "number" && Number.isSafeInteger(v) && v >= 0;
export function parseProjection(
  value: unknown,
  now = Date.now(),
): ProjectionReport | null {
  if (
    !object(value) ||
    value.schema_version !== 1 ||
    value.scope !== "local_projection_observation" ||
    typeof value.available !== "boolean" ||
    value.controls !== "read_only" ||
    value.live_authorized !== false ||
    value.production_certified !== false ||
    typeof value.status !== "string" ||
    typeof value.reason !== "string" ||
    ![
      "unavailable",
      "running",
      "empty",
      "verified",
      "conflict",
      "fenced",
      "timeout",
      "failed",
      "cancelled",
    ].includes(String(value.status))
  )
    return null;
  if (value.observed_at !== null) {
    if (
      typeof value.observed_at !== "string" ||
      value.observed_at.length > 64 ||
      !/^\d{4}-\d{2}-\d{2}T/.test(value.observed_at) ||
      !/(Z|[+-]\d{2}:\d{2})$/.test(value.observed_at)
    )
      return null;
    const [year, month, day] = value.observed_at
      .slice(0, 10)
      .split("-")
      .map(Number);
    const date = new Date(0);
    date.setUTCFullYear(year, month - 1, day);
    if (
      year < 1 ||
      date.getUTCFullYear() !== year ||
      date.getUTCMonth() !== month - 1 ||
      date.getUTCDate() !== day
    )
      return null;
    const age = now - Date.parse(value.observed_at);
    if (!Number.isFinite(age) || age > 60000 || age < -5000) return null;
  }
  if (!value.available) {
    if (
      value.source !== "unavailable" ||
      value.counts !== null ||
      value.last_batch !== null ||
      ["empty", "verified"].includes(String(value.status)) ||
      ![
        "not_attached",
        "not_observed",
        "stale",
        "invalid_observation",
        "operational_error",
      ].includes(String(value.reason))
    )
      return null;
    if (
      value.status !== "unavailable" &&
      (value.observed_at === null || value.reason !== "operational_error")
    )
      return null;
  } else {
    const c = value.counts,
      b = value.last_batch;
    if (
      value.observed_at === null ||
      value.source !== "local_journal" ||
      value.reason !== "none" ||
      !["empty", "verified"].includes(String(value.status)) ||
      !object(c) ||
      !object(b) ||
      Object.keys(c).sort().join() !== "delivered,pending,total" ||
      Object.keys(b).sort().join() !== "acknowledged,checked,inserted" ||
      ![
        c.total,
        c.pending,
        c.delivered,
        b.checked,
        b.inserted,
        b.acknowledged,
      ].every(count)
    )
      return null;
    if (
      (c.total as number) !== (c.pending as number) + (c.delivered as number) ||
      (b.inserted as number) > (b.checked as number) ||
      (b.checked as number) > 100 ||
      b.acknowledged !== b.checked ||
      (c.delivered as number) < (b.acknowledged as number)
    )
      return null;
    if (
      value.status === "empty" &&
      ((b.checked as number) !== 0 ||
        (b.inserted as number) !== 0 ||
        (b.acknowledged as number) !== 0 ||
        c.pending !== 0)
    )
      return null;
    if (value.status === "verified" && b.checked === 0) return null;
  }
  // Return only whitelisted fields. No raw backend extras or arbitrary error text.
  return {
    schema_version: 1,
    scope: "local_projection_observation",
    available: value.available,
    status: value.status as ProjectionReport["status"],
    reason: String(value.reason),
    observed_at: value.observed_at as string | null,
    counts: value.available
      ? (value.counts as ProjectionReport["counts"])
      : null,
    last_batch: value.available
      ? (value.last_batch as ProjectionReport["last_batch"])
      : null,
    source: value.source as ProjectionReport["source"],
    controls: "read_only",
    live_authorized: false,
    production_certified: false,
  };
}
