import AccountTimelinePanel from "./AccountTimelinePanel";
import { useEffect, useState } from "react";
import { consoleFetch } from "./operatorSession";
import { parseAccountObservation, type AccountObservation } from "./accountClient";

export default function AccountPanel() {
  const [observation, setObservation] = useState<AccountObservation | null>(null);
  const [status, setStatus] = useState("Account provider not observed");
  useEffect(() => {
    let stopped = false;
    let controller: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      controller = new AbortController();
      const deadline = setTimeout(() => controller?.abort(), 14000);
      // Clear old numbers before another request; never show stale data as current.
      setObservation(null);
      try {
        const response = await consoleFetch("/console/v1/account", { signal: controller.signal, cache: "no-store" });
        if (!response.ok) throw Error();
        const value = parseAccountObservation(await response.json());
        if (!stopped) { setObservation(value); setStatus("Provider observation received — not a consistent account cut"); }
      } catch {
        if (!stopped) { setObservation(null); setStatus("Account provider unavailable, unattached or stale. No balances inferred."); }
      } finally {
        clearTimeout(deadline);
        if (!stopped) timer = setTimeout(poll, 10000);
      }
    }
    poll();
    return () => { stopped = true; controller?.abort(); clearTimeout(timer); };
  }, []);
  return <><AccountTimelinePanel /><section className="panel" aria-label="Read-only account observations">
    <div className="panel-heading"><h3>Account observation · read only</h3></div>
    <p role="status">{status}</p>
    <p className="muted">Separate from sample portfolio and internal trade ledger. No account activation, protection-order verification, realized PnL or recovery authority.</p>
    {observation && <>
      <p>Observed: {observation.observed_at} · Binance signed REST · amounts preserved as provider decimal strings</p>
      <div className="gate-grid">{Object.entries(observation.totals).map(([key, value]) => <div className="panel" key={key}><strong>{key}</strong><p>{value}</p></div>)}</div>
      <div style={{ overflowX: "auto" }}><table><thead><tr><th>Symbol</th><th>Side</th><th>Quantity</th><th>Entry</th><th>Mark</th><th>Unrealized PnL</th></tr></thead><tbody>{observation.positions.map(p => <tr key={p.symbol + p.side}><td>{p.symbol}</td><td>{p.side}</td><td>{p.quantity}</td><td>{p.entryPrice}</td><td>{p.markPrice}</td><td>{p.unRealizedProfit}</td></tr>)}</tbody></table></div>
      <p className="muted">{observation.limitations.join(" · ")}</p>
    </>}
  </section></>;
}
