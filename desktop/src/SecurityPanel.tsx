import { useCallback, useEffect, useState } from "react";
import { Fingerprint, RefreshCw, ShieldCheck } from "lucide-react";
import {
  performWebAuthnAssertion,
  performWebAuthnRegistration,
} from "./webauthnClient";

type Credential = {
  credential_id: string;
  transports: string[];
  device_type: string;
  backed_up: boolean;
  created_at: number;
  last_used_at: number | null;
};

async function post(path: string, body?: Record<string, unknown>) {
  const response = await fetch(path, {
    method: "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) throw Error("The authenticator operation was rejected.");
  return (await response.json()) as Record<string, unknown>;
}

export default function SecurityPanel() {
  const [credentials, setCredentials] = useState<Credential[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const refresh = useCallback(async () => {
    const response = await fetch("/operator/v1/mfa/credentials", {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) throw Error("Authenticator inventory unavailable.");
    const payload = (await response.json()) as { credentials?: Credential[] };
    if (!Array.isArray(payload.credentials))
      throw Error("Invalid authenticator inventory response.");
    setCredentials(payload.credentials);
  }, []);

  useEffect(() => {
    void refresh().catch((cause) =>
      setError(cause instanceof Error ? cause.message : "Security inventory unavailable."),
    );
  }, [refresh]);

  async function addAuthenticator() {
    if (busy) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const first = await post("/operator/v1/mfa/add/options");
      const assertion = await performWebAuthnAssertion(
        first.options as Record<string, unknown>,
      );
      const second = await post("/operator/v1/mfa/add/verify", {
        challenge: (first.options as Record<string, unknown>).challenge,
        assertion,
      });
      const credential = await performWebAuthnRegistration(
        second.options as Record<string, unknown>,
      );
      await post("/operator/v1/mfa/add/finish", { credential });
      await refresh();
      setMessage("A new authenticator was registered after WebAuthn step-up.");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Authenticator enrollment failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel" aria-label="Operator security and authenticators">
      <div className="panel-heading">
        <div>
          <span className="small-cap">IDENTITY & RECOVERY</span>
          <h3>Authenticators</h3>
        </div>
        <ShieldCheck size={18} />
      </div>
      <p className="muted">
        Add a backup security key only after verifying an existing authenticator.
        Credential revocation is an offline administrator operation, not a browser
        action.
      </p>
      <div className="inline">
        <button
          className="button primary"
          onClick={() => void addAuthenticator()}
          disabled={busy}
        >
          <Fingerprint size={15} />
          {busy ? "Waiting for authenticator…" : "Add authenticator"}
        </button>
        <button
          className="button secondary"
          onClick={() => void refresh().catch((cause) => setError(String(cause)))}
          disabled={busy}
        >
          <RefreshCw size={14} />
          Refresh
        </button>
      </div>
      {credentials.length ? (
        <div className="table-scroll">
          <table>
            <thead>
              <tr>
                <th>Credential</th>
                <th>Device</th>
                <th>Transport</th>
                <th>Backup</th>
              </tr>
            </thead>
            <tbody>
              {credentials.map((item) => (
                <tr key={item.credential_id}>
                  <td className="mono">
                    {item.credential_id.slice(0, 10)}…
                  </td>
                  <td>{item.device_type.replaceAll("_", " ")}</td>
                  <td>{item.transports.join(", ") || "Platform"}</td>
                  <td>{item.backed_up ? "Synced" : "Device-bound"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="muted">No registered authenticator metadata is available.</p>
      )}
      {message && <div className="validation-success" role="status">{message}</div>}
      {error && <div className="validation-error" role="alert">{error}</div>}
      <p className="muted">
        Offline recovery: run <code>scripts/manage_webauthn_credentials.py</code>
        from the trusted host to list or revoke a lost credential. Revoking the
        final credential requires the configured first-device enrollment policy
        before another key can be registered remotely.
      </p>
    </section>
  );
}
