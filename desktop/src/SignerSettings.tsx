import { useState } from "react";
import "./signer.css";

declare global {
  interface Window {
    __TAURI__?: {
      core?: {
        invoke<T>(command: string, args?: Record<string, unknown>): Promise<T>;
      };
    };
  }
}

type AdminResponse = {
  ok: boolean;
  action?: string;
  error?: string;
  account_alias?: string;
  configured?: boolean;
  scope?: "account" | "global";
  mode?: "native" | "external";
  provider_id?: "bitwarden" | "onepassword" | null;
  secret_ref?: string | null;
  credential_pair_ready?: boolean;
};

async function requestSignerAdmin(payload: Record<string, unknown>) {
  const invoke = window.__TAURI__?.core?.invoke;
  if (!invoke) {
    throw new Error("Open this page from the installed AITOS Desktop application.");
  }
  const raw = await invoke<string>("signer_credential_admin", {
    request: JSON.stringify(payload),
  });
  const result = JSON.parse(raw) as AdminResponse;
  if (!result.ok) {
    const messages: Record<string, string> = {
      invalid_signer_admin_request: "The request was rejected.",
      credential_provider_unavailable:
        "The selected credential provider is unavailable or not authenticated.",
      credential_operation_failed:
        "The credential operation failed. No success was confirmed.",
    };
    throw new Error(messages[result.error ?? ""] ?? "Credential operation failed.");
  }
  return result;
}

export default function SignerSettings() {
  const [account, setAccount] = useState("");
  const [scope, setScope] = useState<"account" | "global">("account");
  const [mode, setMode] = useState<"native" | "external">("native");
  const [provider, setProvider] = useState<"bitwarden" | "onepassword">("bitwarden");
  const [reference, setReference] = useState("");
  const [keyId, setKeyId] = useState("");
  const [apiSecret, setApiSecret] = useState("");
  const [confirmSecret, setConfirmSecret] = useState("");
  const [status, setStatus] = useState<AdminResponse | null>(null);
  const [policySaved, setPolicySaved] = useState(false);
  const [trustedHostConfirmed, setTrustedHostConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const ready = Boolean(account.trim()) && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/.test(account.trim());
  const policyTargetReady = scope === "global" || ready;
  const referenceValid =
    provider === "bitwarden"
      ? /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/.test(reference.trim())
      : /^[A-Za-z0-9_-]{4,64}\/[A-Za-z0-9_-]{4,64}$/.test(reference.trim());

  async function run(
    operation: () => Promise<AdminResponse>,
    success: string,
  ): Promise<AdminResponse | null> {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await operation();
      setStatus(result);
      setMessage(success);
      return result;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Credential operation failed.");
      return null;
    } finally {
      setBusy(false);
    }
  }

  function loadStatus() {
    return run(
      async () => {
        const result = await requestSignerAdmin({
          action: "status",
          account_alias: account.trim(),
        });
        setPolicySaved(Boolean(result.configured));
        if (result.scope) setScope(result.scope);
        if (result.configured && result.mode) setMode(result.mode);
        if (result.provider_id) setProvider(result.provider_id);
        if (result.secret_ref) setReference(result.secret_ref);
        return result;
      },
      "Signer status loaded. Secret values were not returned to this window.",
    );
  }

  function applyMode() {
    return run(
      async () => {
        const result = await requestSignerAdmin({
          action: "set-mode",
          scope,
          ...(scope === "account" ? { account_alias: account.trim() } : {}),
          mode,
          ...(mode === "external"
            ? { provider_id: provider, secret_ref: reference.trim() }
            : {}),
        });
        setPolicySaved(true);
        return result;
      },
      scope === "global"
        ? "Global credential default saved. Existing account overrides were not changed."
        : "Account credential policy saved. Existing credentials were not migrated.",
    );
  }

  function storeCredentials(event: React.FormEvent) {
    event.preventDefault();
    if (apiSecret !== confirmSecret) {
      setError("The API secret confirmation does not match.");
      return;
    }
    return run(
      () =>
        requestSignerAdmin({
          action: "store-credentials",
          account_alias: account.trim(),
          key_id: keyId,
          api_secret: apiSecret,
        }),
      "Credential pair stored and read back through the selected provider.",
    ).then((result) => {
      // Clear the secret fields only after the signer confirms read-back.
      if (result) {
        setKeyId("");
        setApiSecret("");
        setConfirmSecret("");
      }
    });
  }

  const nativeApiAvailable = Boolean(window.__TAURI__?.core?.invoke);
  const available = nativeApiAvailable && trustedHostConfirmed;

  return (
    <main className="signer-page">
      <header className="signer-header">
        <div className="signer-brand"><span className="signer-mark">A</span><div><strong>AITOS</strong><small>TRUSTED SIGNER</small></div></div>
        <span className="signer-local-badge">LOCAL DEVICE ONLY</span>
      </header>
      <section className="signer-hero">
        <div className="signer-eyebrow">CREDENTIAL CUSTODY · ACCOUNT POLICY</div>
        <h1>Signer settings</h1>
        <p>Manage exchange credentials on this trusted device. This window is separate from the remote operator console.</p>
      </section>
      <section className="signer-warning" role="note">
        <strong>Trust boundary</strong>
        <p>Never run the signer or unlock Bitwarden/1Password on a provider-managed RDP/VPS. Its administrator may inspect process memory and unlocked vault sessions. Keep this signer on a separately controlled device.</p>
        <label className="signer-trust-confirm"><input type="checkbox" checked={trustedHostConfirmed} onChange={e => { setTrustedHostConfirmed(e.target.checked); if (!e.target.checked) { setKeyId(""); setApiSecret(""); setConfirmSecret(""); } }} /> I confirm this is a user-controlled trusted device, not a provider-managed RDP/VPS.</label>
      </section>
      {!nativeApiAvailable && <div className="signer-error">Native signer commands are unavailable. Open this page from the installed AITOS Desktop application.</div>}
      {nativeApiAvailable && !trustedHostConfirmed && <div className="signer-error">Confirm the trusted-host boundary before enabling credential operations.</div>}
      <section className="signer-card">
        <div className="signer-card-heading"><div><span className="signer-eyebrow">01 / ACCOUNT</span><h2>Choose the account</h2></div><span className="signer-step">01</span></div>
        <label htmlFor="account-alias">Account alias</label>
        <input id="account-alias" value={account} onChange={e => { setAccount(e.target.value); setKeyId(""); setApiSecret(""); setConfirmSecret(""); if (scope === "account") setPolicySaved(false); setStatus(null); }} placeholder="e.g. binance-testnet-primary" autoComplete="off" spellCheck={false} maxLength={64}/>
        <p className="signer-help">Use the exact same alias as AITOS_SIGNER_ACCOUNT_ALIAS in the signer service configuration. The desktop and signer service must run as the same OS user and use the same AITOS_SIGNER_DATA_DIR.</p>
        <div className="signer-actions">
          <button className="signer-button secondary" disabled={!ready || busy || !available} onClick={() => void loadStatus()}>Load current policy</button>
        </div>
        {status?.action === "status" && <div className="signer-status"><span>Mode: <b>{status.mode ?? "not configured"}</b></span><span>Credential pair: <b>{status.credential_pair_ready ? "available" : "not verified"}</b></span>{status.mode === "external" && <span>Provider: <b>{status.provider_id}</b></span>}</div>}
      </section>
      <section className="signer-card">
        <div className="signer-card-heading"><div><span className="signer-eyebrow">02 / CUSTODY</span><h2>Choose where credentials live</h2></div><span className="signer-step">02</span></div>
        <div className="signer-scope-grid">
          <button type="button" className={scope === "account" ? "signer-scope selected" : "signer-scope"} onClick={() => { setScope("account"); setPolicySaved(false); }} aria-pressed={scope === "account"}>Per-account override</button>
          <button type="button" className={scope === "global" ? "signer-scope selected" : "signer-scope"} onClick={() => { setScope("global"); setPolicySaved(false); }} aria-pressed={scope === "global"}>Global default</button>
        </div>
        <p className="signer-help">{scope === "account" ? "Applies only to the account alias above. Other accounts keep their own policy or the global default." : "Applies to accounts without an explicit per-account override. Native OS slots remain account-specific."}</p>
        <div className="signer-mode-grid">
          <button type="button" className={mode === "native" ? "signer-mode selected" : "signer-mode"} onClick={() => { setMode("native"); setPolicySaved(false); }} aria-pressed={mode === "native"}><strong>Native OS vault</strong><span>Windows Credential Manager · macOS Keychain · Linux Secret Service</span></button>
          <button type="button" className={mode === "external" ? "signer-mode selected" : "signer-mode"} onClick={() => { setMode("external"); setPolicySaved(false); }} aria-pressed={mode === "external"}><strong>External password manager</strong><span>Bitwarden CLI or 1Password CLI on this trusted device</span></button>
        </div>
        {mode === "external" && <div className="signer-fields">
          <label htmlFor="provider">Provider</label>
          <select id="provider" value={provider} onChange={e => { setProvider(e.target.value as "bitwarden" | "onepassword"); setPolicySaved(false); }}><option value="bitwarden">Bitwarden</option><option value="onepassword">1Password</option></select>
          <label htmlFor="secret-reference">{scope === "global" ? "Shared global item reference" : "Dedicated account item reference"}</label>
          <input id="secret-reference" value={reference} onChange={e => { setReference(e.target.value); setPolicySaved(false); }} placeholder={provider === "bitwarden" ? "Bitwarden item UUID" : "vault-id/item-id"} autoComplete="off" spellCheck={false} maxLength={512}/>
          <p className="signer-help">Create a dedicated item first. AITOS uses only its concealed fields <code>aitos_exchange_key_id</code> and <code>aitos_exchange_api_secret</code>. Launch AITOS Desktop from a shell/session where the provider CLI authentication is already available; its session environment is passed only to the provider child process.</p>
        </div>}
        <div className="signer-actions">
          <button className="signer-button primary" disabled={!policyTargetReady || busy || !available || (mode === "external" && !referenceValid)} onClick={() => void applyMode()}>{busy ? "Working…" : scope === "global" ? "Save global default" : "Save account override"}</button>
        </div>
        {mode === "external" && scope === "global" && <p className="signer-shared-warning">A global External reference is shared by every account without its own override. Use a dedicated per-account override when accounts have different exchange API keys.</p>}
        <p className="signer-help">Changing this policy does not copy or delete credentials in the previous vault. Store and verify a credential pair below before restarting the signer.</p>
      </section>
      <section className="signer-card">
        <div className="signer-card-heading"><div><span className="signer-eyebrow">03 / PROVISION</span><h2>Store Testnet credentials</h2></div><span className="signer-step">03</span></div>
        <form onSubmit={storeCredentials}>
          <label htmlFor="exchange-key">Binance Futures Testnet API key</label>
          <input id="exchange-key" type="password" autoComplete="new-password" value={keyId} onChange={e => setKeyId(e.target.value)} spellCheck={false} maxLength={128} required disabled={!available || busy}/>
          <label htmlFor="exchange-secret">API secret</label>
          <input id="exchange-secret" type="password" value={apiSecret} onChange={e => setApiSecret(e.target.value)} autoComplete="new-password" maxLength={2560} required disabled={!available || busy}/>
          <label htmlFor="exchange-secret-confirm">Confirm API secret</label>
          <input id="exchange-secret-confirm" type="password" value={confirmSecret} onChange={e => setConfirmSecret(e.target.value)} autoComplete="new-password" maxLength={2560} required disabled={!available || busy}/>
          <div className="signer-help">Secrets are sent only to the local native signer command over Tauri IPC, then passed to the credential sidecar via stdin. They are never placed in command-line arguments, browser storage, logs or the operator console. Fields clear after the operation.</div>
          <div className="signer-actions"><button className="signer-button primary" type="submit" disabled={!ready || !policySaved || busy || !available || !keyId || !apiSecret || !confirmSecret}>{busy ? "Storing securely…" : "Store and verify credential pair"}</button></div>
        </form>
      </section>
      {message && <div className="signer-success" role="status">{message}</div>}
      {error && <div className="signer-error" role="alert">{error}</div>}
      <footer className="signer-footer">AITOS · LOCAL-FIRST · PAPER / TESTNET CREDENTIAL SETUP ONLY</footer>
    </main>
  );
}
