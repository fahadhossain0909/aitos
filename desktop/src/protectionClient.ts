export const protectionStatuses = [
  "unowned_order",
  "mismatch",
  "not_in_open_inventory",
  "unapplied_match",
  "applied_order_present",
] as const;
export type ProtectionObservation = {
  schema_version: 1;
  source: "caller_exported_protection_inventory";
  observed_at: string;
  captured_at: string;
  controls: "read_only";
  safe_to_resume: false;
  account_exposure_verified: false;
  automatic_actions: [];
  counts: Record<string, number>;
  findings: { id: string; symbol: string; status: string; reasons: string[] }[];
  limitations: string[];
};
export function parseProtectionObservation(
  input: unknown,
  now = Date.now(),
): ProtectionObservation {
  const s = input as ProtectionObservation | null;
  if (
    !s ||
    s.schema_version !== 1 ||
    s.source !== "caller_exported_protection_inventory" ||
    s.controls !== "read_only" ||
    s.safe_to_resume !== false ||
    s.account_exposure_verified !== false ||
    !Array.isArray(s.automatic_actions) ||
    s.automatic_actions.length !== 0 ||
    !Array.isArray(s.findings) ||
    s.findings.length > 10000 ||
    !s.counts ||
    Object.keys(s.counts).length !== protectionStatuses.length
  )
    throw Error("Invalid protection evidence");
  for (const [stamp, maxAge] of [
    [s.observed_at, 30000],
    [s.captured_at, 120000],
  ] as const) {
    if (typeof stamp !== "string" || !/(Z|[+-]\d{2}:\d{2})$/.test(stamp))
      throw Error("Invalid evidence timestamp");
    const age = now - Date.parse(stamp);
    if (!Number.isFinite(age) || age < 0 || age > maxAge)
      throw Error("Stale protection evidence");
  }
  const counts: Record<string, number> = Object.fromEntries(
    protectionStatuses.map((k) => [k, 0]),
  );
  const ids = new Set<string>();
  for (const row of s.findings) {
    if (
      !row ||
      typeof row.id !== "string" ||
      !/^[a-f0-9]{24}$/.test(row.id) ||
      ids.has(row.id) ||
      !/^[A-Z0-9]{2,24}$/.test(row.symbol) ||
      !protectionStatuses.includes(
        row.status as (typeof protectionStatuses)[number],
      ) ||
      !Array.isArray(row.reasons) ||
      row.reasons.length > 20 ||
      !row.reasons.every((v) => typeof v === "string" && v.length <= 200)
    )
      throw Error("Invalid protection finding");
    ids.add(row.id);
    counts[row.status]++;
  }
  if (
    !protectionStatuses.every(
      (k) => Number.isSafeInteger(s.counts[k]) && counts[k] === s.counts[k],
    ) ||
    !Array.isArray(s.limitations) ||
    s.limitations.length > 20 ||
    !s.limitations.every((v) => typeof v === "string" && v.length <= 160)
  )
    throw Error("Invalid evidence summary");
  return s;
}
