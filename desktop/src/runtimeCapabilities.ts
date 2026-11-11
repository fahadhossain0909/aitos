export const capabilityPaths: Record<string, string | null> = {
  "runtime.snapshot": "/console/v1/snapshot",
  "ledger.history": "/console/v1/history",
  "ledger.performance_window": "/console/v1/performance",
  "research.plan": "/console/v1/research/plan",
  "persistence.shadow": null,
  "account.equity_timeline": null,
  "account.margin": null,
  "market.mark_price": null,
  "orders.protection": null,
  "alerts.delivery": null,
  "ledger.consistent_cursor": null,
  "configuration.approval": null,
  "runtime.control": null,
};
export type RuntimeCapabilities = {
  api_version: "1.0";
  capabilities: { id: string; path: string | null; available: boolean }[];
  modules: string[];
  limitations: string[];
};
export function parseRuntimeCapabilities(input: unknown, now = Date.now()): RuntimeCapabilities {
  const s = input as Record<string, unknown> | null;
  if (!s || s.schema_version !== 1 || s.api_version !== "1.0" ||
      s.controls !== "read_only" || s.live_authorized !== false ||
      s.production_certified !== false || typeof s.generated_at !== "string" ||
      !/(Z|[+-]\d{2}:\d{2})$/.test(s.generated_at) ||
      !Array.isArray(s.capabilities) || s.capabilities.length !== Object.keys(capabilityPaths).length ||
      !Array.isArray(s.modules) || s.modules.length > 100 ||
      !s.modules.every(m => typeof m === "string" && /^[A-Za-z0-9_.:-]{1,100}$/.test(m)) ||
      !Array.isArray(s.limitations) || !s.limitations.every(m => typeof m === "string" && m.length <= 120))
    throw Error("Incompatible runtime API: read-only console protocol 1.0 required");
  const age = now - Date.parse(s.generated_at);
  if (!Number.isFinite(age) || age < -5000 || age > 15000)
    throw Error("Stale runtime capability evidence");
  const seen = new Set<string>();
  for (const row of s.capabilities) {
    if (!row || typeof row.id !== "string" || !Object.hasOwn(capabilityPaths, row.id) ||
        seen.has(row.id) || row.path !== capabilityPaths[row.id] || typeof row.available !== "boolean")
      throw Error("Invalid runtime capability contract");
    // Capability reports never install arbitrary endpoints or grant mutation rights.
    if (["configuration.approval", "runtime.control"].includes(row.id) && row.available)
      throw Error("Unsupported control-plane authority");
    seen.add(row.id);
  }
  return s as unknown as RuntimeCapabilities;
}
