/**
 * Safe defaults for the unauthenticated Vite development server.
 *
 * Remote previews require both an explicit bind host and an explicit hostname
 * allowlist. This prevents accidental LAN exposure and avoids disabling Vite's
 * Host-header/DNS-rebinding protection by default.
 */
export type DevServerPolicy = {
  host: string;
  allowedHosts?: string[];
};

const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

export function getDevServerPolicy(
  env: Record<string, string | undefined>,
): DevServerPolicy {
  const host = env.AITOS_VITE_HOST?.trim() || "127.0.0.1";
  const allowedHosts = (env.AITOS_VITE_ALLOWED_HOSTS ?? "")
    .split(",")
    .map((value) => value.trim().toLowerCase())
    .filter(Boolean);

  if (
    allowedHosts.some(
      (value) =>
        value === "*" ||
        value.includes("://") ||
        value.includes("/") ||
        value.includes(":"),
    )
  ) {
    throw new Error(
      "AITOS_VITE_ALLOWED_HOSTS must contain exact hostnames, not wildcards, URLs, paths, or ports",
    );
  }
  if (!LOOPBACK_HOSTS.has(host.toLowerCase()) && allowedHosts.length === 0) {
    throw new Error("Non-loopback Vite binding requires AITOS_VITE_ALLOWED_HOSTS");
  }

  return allowedHosts.length > 0 ? { host, allowedHosts } : { host };
}
