import { useEffect, useState } from "react";
import { consoleFetch } from "./operatorSession";
import {
  parseProtectionObservation,
  type ProtectionObservation,
} from "./protectionClient";

export default function ProtectionPanel() {
  const [evidence, setEvidence] = useState<ProtectionObservation | null>(null);
  const [status, setStatus] = useState("Protection evidence not observed");
  useEffect(() => {
    let stopped = false;
    let controller: AbortController | undefined;
    let timer: ReturnType<typeof setTimeout>;
    let expiry: ReturnType<typeof setTimeout>;
    async function poll() {
      clearTimeout(expiry);
      controller = new AbortController();
      const timeout = setTimeout(() => controller?.abort(), 12000);
      setEvidence(null);
      try {
        const response = await consoleFetch("/console/v1/protection", {
          signal: controller.signal,
          cache: "no-store",
        });
        if (!response.ok) throw Error();
        const result = parseProtectionObservation(await response.json());
        if (!stopped) {
          setEvidence(result);
          expiry = setTimeout(
            () => {
              setEvidence(null);
              setStatus("Protection evidence expired. Exposure is unknown.");
            },
            Math.max(
              0,
              Math.min(
                Date.parse(result.observed_at) + 30000,
                Date.parse(result.captured_at) + 120000,
              ) - Date.now(),
            ),
          );
          setStatus(
            "Exported protection inventory compared — no protection guarantee or resume approval",
          );
        }
      } catch {
        if (!stopped) {
          setEvidence(null);
          setStatus(
            "Protection evidence unavailable, stale or incomplete. Exposure is unknown.",
          );
        }
      } finally {
        clearTimeout(timeout);
        if (!stopped) timer = setTimeout(poll, 10000);
      }
    }
    poll();
    return () => {
      stopped = true;
      controller?.abort();
      clearTimeout(timer);
      clearTimeout(expiry);
    };
  }, []);
  return (
    <section className="panel" aria-label="Protection evidence findings">
      <div className="panel-heading">
        <h3>Protective-order evidence · read only</h3>
      </div>
      <p role="status">{status}</p>
      <p className="muted">
        Caller-exported inventory, provenance not authenticated. Missing orders
        may be filled, canceled, expired or absent. No replacement,
        acknowledgement, quarantine clearing or trading action is exposed.
      </p>
      {evidence && (
        <>
          <p>Captured: {evidence.captured_at}</p>
          <div className="gate-grid">
            {Object.entries(evidence.counts).map(([k, v]) => (
              <div className="panel" key={k}>
                <strong>{k}</strong>
                <p>{v}</p>
              </div>
            ))}
          </div>
          <p>
            Findings are local evidence only. External alert delivery is not
            configured.
          </p>
          <div style={{ overflowX: "auto" }}>
            <table>
              <thead>
                <tr>
                  <th>Reference</th>
                  <th>Symbol</th>
                  <th>Finding</th>
                  <th>Evidence gaps</th>
                </tr>
              </thead>
              <tbody>
                {evidence.findings.slice(0, 100).map((f) => (
                  <tr key={f.id}>
                    <td>{f.id}</td>
                    <td>{f.symbol}</td>
                    <td>{f.status}</td>
                    <td>{f.reasons.join(" · ")}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {evidence.findings.length > 100 && (
            <p>
              Showing first 100 of {evidence.findings.length} findings. Full
              bounded inventory requires operator review.
            </p>
          )}
        </>
      )}
    </section>
  );
}
