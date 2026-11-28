import { useEffect, useState } from "react";
import { Fingerprint, LockKeyhole, ShieldCheck } from "lucide-react";
import { performWebAuthnAssertion } from "./webauthnClient";

type Challenge = {
  challenge_id: string;
  action_digest: string;
  expires_at: number;
  options: Record<string, unknown>;
};

export default function ApprovalPanel() {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [executionScope, setExecutionScope] = useState<string | null>(null);
  const [actionText, setActionText] = useState("");
  const [challenge, setChallenge] = useState<Challenge | null>(null);
  const [approvalId, setApprovalId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    void fetch("/operator/v1/approval/capabilities", {
      credentials: "same-origin",
      cache: "no-store",
    })
      .then(async (response) => {
        if (!response.ok) throw Error("Approval capabilities unavailable");
        return (await response.json()) as {
          enabled?: boolean;
          execution_scope?: string | null;
        };
      })
      .then((value) => {
        if (active) {
          setEnabled(value.enabled === true);
          setExecutionScope(value.execution_scope ?? null);
        }
      })
      .catch(() => {
        if (active) setEnabled(false);
      });
    return () => {
      active = false;
    };
  }, []);

  async function requestApproval() {
    if (busy) return;
    setBusy(true);
    setError("");
    setApprovalId("");
    setChallenge(null);
    try {
      const action = JSON.parse(actionText) as unknown;
      if (!action || typeof action !== "object" || Array.isArray(action))
        throw Error("Enter one canonical action JSON object.");
      const response = await fetch("/operator/v1/approval/challenge", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      if (!response.ok) throw Error("The approval request was rejected.");
      setChallenge((await response.json()) as Challenge);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Approval request failed.");
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!challenge || busy) return;
    setBusy(true);
    setError("");
    try {
      const assertion = await performWebAuthnAssertion(challenge.options);
      const response = await fetch("/operator/v1/approval/approve", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          challenge_id: challenge.challenge_id,
          assertion,
        }),
      });
      if (!response.ok) throw Error("WebAuthn step-up approval was rejected.");
      const token = (await response.json()) as { token_id?: string };
      if (!token.token_id) throw Error("The gateway returned no approval receipt.");
      setApprovalId(token.token_id);
      setChallenge(null);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Approval failed.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel" aria-label="Authenticated approval desk">
      <div className="panel-heading">
        <div>
          <span className="small-cap">AUTHENTICATED CONTROL</span>
          <h3>Approval desk</h3>
        </div>
        <ShieldCheck size={18} />
      </div>
      <p className="muted">
        Reviews a canonical action digest and issues a short-lived, single-use
        WebAuthn approval receipt. This interface does not submit or execute orders.
      </p>
      {enabled === null ? (
        <p className="muted">Checking approval policy…</p>
      ) : !enabled || !executionScope ? (
        <div className="empty">
          <LockKeyhole size={24} />
          <h3>Approval service not configured</h3>
          <p>
            A server-side RBAC authorizer, approval signing key and shared
            execution account scope are required. No approval or execution
            control is exposed.
          </p>
        </div>
      ) : (
        <>
          <p className="muted">
            Target account scope: <code>{executionScope}</code>. Include this exact
            value in the action JSON; venue side must be BUY or SELL.
          </p>
          <label htmlFor="approval-action">Canonical action JSON</label>
          <textarea
            id="approval-action"
            value={actionText}
            onChange={(event) => setActionText(event.target.value)}
            rows={6}
            spellCheck={false}
            placeholder='{"action_type":"...","resource_id":"..."}'
            disabled={busy || !!challenge}
          />
          <div className="inline">
            <button
              className="button primary"
              onClick={() => void requestApproval()}
              disabled={busy || !!challenge || !actionText.trim()}
            >
              Request approval
            </button>
            {challenge && (
              <button
                className="button primary"
                onClick={() => void approve()}
                disabled={busy}
              >
                <Fingerprint size={15} />
                Verify & approve
              </button>
            )}
          </div>
          {challenge && (
            <div className="validation-info" role="status">
              <strong>Action digest</strong>
              <code>{challenge.action_digest}</code>
              <span>Challenge expires at {new Date(challenge.expires_at * 1000).toLocaleTimeString()}.</span>
              Review the action in the JSON editor before using your authenticator.
            </div>
          )}
          {approvalId && (
            <div className="validation-success" role="status">
              Signed approval receipt issued: <code>{approvalId}</code>
              <br />
              The receipt is not an order and is not execution confirmation.
            </div>
          )}
        </>
      )}
      {error && <div className="validation-error" role="alert">{error}</div>}
    </section>
  );
}
