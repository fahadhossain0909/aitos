import { useEffect, useRef, useState } from "react";
import { consoleFetch } from "./operatorSession";
import { parseTimelinePage, type TimelinePage } from "./accountTimelineClient";

export default function AccountTimelinePanel() {
  const [page, setPage] = useState<TimelinePage | null>(null);
  const [status, setStatus] = useState("Account timeline not loaded");
  const [busy, setBusy] = useState(false);
  const active = useRef<AbortController | null>(null);
  async function load(cursor?: string) {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setPage(null);
    const deadline = setTimeout(() => controller.abort(), 7000);
    try {
      const query = new URLSearchParams({ limit: "50" });
      if (cursor) query.set("cursor", cursor);
      const response = await consoleFetch("/console/v1/account/timeline?" + query, {signal:controller.signal,cache:"no-store"});
      if (!response.ok) throw Error();
      const result = parseTimelinePage(await response.json());
      if (active.current === controller && !controller.signal.aborted) { setPage(result); setStatus("Persisted observations — pinned insert cut " + result.upper_id); }
    } catch {
      if (active.current === controller) setStatus("Timeline unavailable or cursor expired. Start a new cut to retry.");
    } finally {
      clearTimeout(deadline);
      if (active.current === controller) setBusy(false);
    }
  }
  useEffect(() => { load(); return () => { active.current?.abort(); active.current = null; }; }, []);
  return <section className="panel" aria-label="Durable account timeline">
    <div className="panel-heading"><h3>Account equity & margin observations</h3><button className="button" disabled={busy} onClick={() => load()}>Start new cut</button></div>
    <p role="status">{status}</p>
    <p className="muted">Only observations recorded by the selected runtime. Not all-time returns, provider-atomic equity, or a consistent trade-ledger history. New records do not move this cut.</p>
    {page && <><div style={{overflowX:"auto"}}><table><thead><tr><th>Observed at</th><th>Wallet</th><th>Margin equity</th><th>Unrealized</th><th>Initial margin</th><th>Maintenance margin</th><th>Available</th></tr></thead><tbody>{page.rows.map(row => <tr key={row.id}><td>{row.observed_at}</td>{["totalWalletBalance","totalMarginBalance","totalUnrealizedProfit","totalInitialMargin","totalMaintMargin","availableBalance"].map(k=><td key={k}>{row.totals[k]}</td>)}</tr>)}</tbody></table></div><button className="button" disabled={busy || !page.next_cursor} onClick={() => load(page.next_cursor!)}>Older observations</button></>}
  </section>;
}
