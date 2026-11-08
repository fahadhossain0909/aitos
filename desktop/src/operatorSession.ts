export type OperatorSession = {
  schema_version: 1;
  gateway: true;
  authenticated: boolean;
  expires_in_seconds: number;
  controls: "read_only";
};
export function parseOperatorSession(value: unknown): OperatorSession {
  const s = value as Partial<OperatorSession> | null;
  if (
    !s ||
    s.schema_version !== 1 ||
    s.gateway !== true ||
    s.controls !== "read_only" ||
    typeof s.authenticated !== "boolean" ||
    typeof s.expires_in_seconds !== "number" ||
    !Number.isInteger(s.expires_in_seconds) ||
    s.expires_in_seconds < 0 ||
    s.expires_in_seconds > 3600 ||
    s.authenticated !== s.expires_in_seconds > 0
  )
    throw Error("Invalid operator session");
  return s as OperatorSession;
}
export async function consoleFetch(path: string, init: RequestInit = {}) {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    cache: "no-store",
  });
  if (response.status === 401)
    window.dispatchEvent(new Event("aitos:operator-session-expired"));
  return response;
}
