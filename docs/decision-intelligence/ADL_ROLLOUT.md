# ADL integration and rollout controls

## Scanner shadow path

OpportunityScanner accepts optional adl_runtime and adl_spec dependencies. Configure both or neither. Candidate evidence is passed to ADL after the existing scanner candidate has been scored. The candidate is returned unchanged regardless of ADL output. Results are emitted as decision.adl_shadow_evaluated and persisted by LearningExperienceRecorder to the existing ClickHouse learning-experience store.

## Provider boundary

JevDecisionProvider accepts an injected callable. No vendor endpoint, authentication format, SDK package, or model identifier is guessed. Verify the private provider's official contract, timeouts, retries, data retention, and output schema before enabling a Jev transport. NativeEvidenceProvider supplies a transparent heuristic prior from scanner direction and composite score; it is not a trained model and must not be treated as calibrated. Scanner composite scores are evidence, not calibrated probabilities.

## Replay and governance

replay_chronologically sorts observations by snapshot time and updates calibration only after evaluating each observation. Use point-in-time features and matured labels. Challenger assessment requires out-of-sample Brier/ECE and sample sufficiency. Eligibility is a report only: it never promotes a model or enables order execution.

## Operational rollout

1. Run unit, integration, formatting, type and security checks.
2. Replay historical data with fees, funding, slippage, latency and partial fills represented in outcome labels.
3. Compare native, Jev (when official access is available), and ensemble outputs on identical point-in-time snapshots.
4. Run paper shadow canaries with explicit operator approval and rollback.
5. Keep execution_eligible=false; the existing risk and execution pipeline remains the sole authority for orders.
