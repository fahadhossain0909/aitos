import { useEffect, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  ArrowRight,
  KeyRound,
  LockKeyhole,
  LogOut,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { parseOperatorSession } from "./operatorSession";
type Mode =
  "checking" | "development" | "authenticated" | "signed_out" | "error";
export default function OperatorBoundary({
  children,
}: {
  children: ReactNode;
}) {
  const [mode, setMode] = useState<Mode>("checking"),
    [expires, setExpires] = useState(0),
    [clock, setClock] = useState(Date.now()),
    [password, setPassword] = useState(""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [logoutFailed, setLogoutFailed] = useState(false);
  const mounted = useRef(true),
    requestId = useRef(0),
    controller = useRef<AbortController | null>(null);
  async function check() {
    const id = ++requestId.current;
    controller.current?.abort();
    const c = new AbortController();
    controller.current = c;
    const timer = setTimeout(() => c.abort(), 5000);
    try {
      const r = await fetch("/operator/v1/session", {
        credentials: "same-origin",
        cache: "no-store",
        signal: c.signal,
      });
      if (
        import.meta.env.DEV &&
        r.ok &&
        r.headers.get("content-type")?.includes("text/html")
      ) {
        if (mounted.current && id === requestId.current) setMode("development");
        return;
      }
      if (!r.ok) throw Error();
      const s = parseOperatorSession(await r.json());
      if (mounted.current && id === requestId.current) {
        setClock(Date.now());
        setExpires(Date.now() + s.expires_in_seconds * 1000);
        setMode(s.authenticated ? "authenticated" : "signed_out");
        setError("");
        setLogoutFailed(false);
      }
    } catch {
      if (mounted.current && id === requestId.current) {
        setMode("error");
        setError(
          "The protected operator gateway could not be verified. Financial views are hidden.",
        );
      }
    } finally {
      clearTimeout(timer);
    }
  }
  useEffect(() => {
    mounted.current = true;
    void check();
    return () => {
      mounted.current = false;
      requestId.current++;
      controller.current?.abort();
    };
  }, []);
  useEffect(() => {
    if (mode !== "authenticated") return;
    const timer = setInterval(() => {
      const now = Date.now();
      setClock(now);
      if (now >= expires) {
        setMode("signed_out");
        setPassword("");
        setError("Your absolute session lifetime ended. Sign in again.");
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [mode, expires]);
  useEffect(() => {
    const expire = () => {
      if (mode === "authenticated") {
        setMode("signed_out");
        setPassword("");
        setError(
          "Your operator session expired or was revoked. Financial views were cleared.",
        );
      }
    };
    window.addEventListener("aitos:operator-session-expired", expire);
    return () =>
      window.removeEventListener("aitos:operator-session-expired", expire);
  }, [mode]);
  async function signIn(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    const c = new AbortController();
    controller.current = c;
    const timer = setTimeout(() => c.abort(), 8000);
    try {
      const r = await fetch("/operator/v1/login", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
        signal: c.signal,
        cache: "no-store",
      });
      if (!r.ok)
        throw Error(
          r.status === 429
            ? "Too many sign-in attempts. Wait at least one minute."
            : r.status === 401
              ? "Operator sign-in was rejected."
              : "Protected sign-in is unavailable.",
        );
      setPassword("");
      await check();
    } catch (e) {
      if (mounted.current)
        setError(
          e instanceof Error && e.name !== "AbortError"
            ? e.message
            : "Sign-in timed out. No session was confirmed.",
        );
    } finally {
      clearTimeout(timer);
      if (mounted.current) {
        setBusy(false);
        setPassword("");
      }
    }
  }
  async function signOut() {
    if (busy) return;
    setBusy(true);
    setMode("checking");
    setPassword("");
    const c = new AbortController();
    controller.current?.abort();
    controller.current = c;
    const timer = setTimeout(() => c.abort(), 8000);
    try {
      const r = await fetch("/operator/v1/logout", {
        method: "POST",
        credentials: "same-origin",
        cache: "no-store",
        signal: c.signal,
      });
      if (r.status !== 204) throw Error();
      if (mounted.current) {
        setMode("signed_out");
        setError("");
        setLogoutFailed(false);
      }
    } catch {
      if (mounted.current) {
        setMode("error");
        setLogoutFailed(true);
        setError(
          "Financial views were hidden, but server-side session revocation was not confirmed. Retry sign-out; do not assume the session is revoked.",
        );
      }
    } finally {
      clearTimeout(timer);
      if (mounted.current) setBusy(false);
    }
  }
  if (mode === "development") return <>{children}</>;
  if (mode === "authenticated") {
    const seconds = Math.max(0, Math.ceil((expires - clock) / 1000));
    return (
      <div className="authenticated-shell">
        <div className="operator-session-bar">
          <span>
            <ShieldCheck size={12} />
            Authenticated · read-only
          </span>
          <span className="mono">
            Session {Math.floor(seconds / 60)}:
            {String(seconds % 60).padStart(2, "0")}
          </span>
          <button onClick={signOut}>
            <LogOut size={12} />
            Sign out
          </button>
        </div>
        {children}
      </div>
    );
  }
  return (
    <main className="operator-lock-screen">
      <section className="operator-lock-card">
        <div className="operator-lock-brand">
          <img src="/icon.svg" alt="" />
          <span>
            aitos<span>.</span>
          </span>
          <span className="pill">OPERATOR ACCESS</span>
        </div>
        <div className="operator-lock-icon">
          <LockKeyhole size={27} />
        </div>
        <div className="eyebrow">INTENTIONAL ACCESS. EXPLICIT CONTROL.</div>
        <h1>
          {mode === "checking"
            ? "Verifying the boundary."
            : mode === "error"
              ? "A verified session first."
              : "A considered workspace.\nA protected session."}
        </h1>
        <p>
          This sign-in protects the read-only operator console. It is not an
          exchange login, an API-key vault or permission to trade.
        </p>
        {error && (
          <div className="validation-error" role="alert">
            {error}
          </div>
        )}
        {mode === "signed_out" ? (
          <form onSubmit={signIn}>
            <label htmlFor="operator-password">Operator password</label>
            <div className="operator-password-field">
              <KeyRound size={16} />
              <input
                id="operator-password"
                name="operator-password"
                type="password"
                autoComplete="current-password"
                autoFocus
                value={password}
                maxLength={1024}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="Your configured operator password"
                required
                disabled={busy}
              />
            </div>
            <button
              className="button primary full"
              disabled={busy || !password}
            >
              {busy ? "Verifying access…" : "Open read-only workspace"}
              <ArrowRight size={16} />
            </button>
          </form>
        ) : mode === "error" ? (
          <button
            className="button secondary full"
            disabled={busy}
            onClick={() => (logoutFailed ? void signOut() : void check())}
          >
            <RefreshCw size={15} />
            {logoutFailed
              ? "Retry session revocation"
              : "Retry gateway verification"}
          </button>
        ) : (
          <div className="operator-verifying">
            <RefreshCw size={15} />
            Checking the local operator boundary…
          </div>
        )}
        <div className="operator-lock-foot">
          <ShieldCheck size={14} />
          <span>HttpOnly session · absolute expiry · no live activation</span>
        </div>
      </section>
      <span className="operator-lock-caption mono">
        AITOS OS / LOCAL-FIRST / EXPLICIT AUTHORITY
      </span>
    </main>
  );
}
