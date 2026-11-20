# AITOS Desktop Operator Console — User Manual

## 1. What this application is

AITOS has two desktop delivery tracks:

- **Native Console Session Manager (Windows/Linux):** launches the authenticated read-only operator console in the default browser and connects to an already-running AITOS backend.
- **Tauri Embedded Desktop (Windows/Linux/macOS candidate):** packages the React operator workspace in a native shell and includes a separate local Trusted Signer Settings window.

Both are operator-facing applications. Neither installs or starts the AITOS trading backend. The Tauri packages are unsigned evaluation candidates, not signed/trusted production releases. See [Installation & Deployment](../INSTALLATION.md) for platform-specific setup and artifact retrieval.

## 2. First launch and connection

### 2.1 Start with an existing backend

For local paper/runtime monitoring, start the backend separately and verify its health endpoint:

```bash
curl http://127.0.0.1:8090/health
```

The backend must have a strong `AITOS_CONSOLE_TOKEN` configured before authenticated console routes are enabled. The desktop client stores its operator password and backend bearer in the operating system credential provider; it does not store them in browser local/session storage.

### 2.2 Connect to a local backend

Use the backend origin:

```text
http://127.0.0.1:8090
```

Create a unique operator password when prompted and provide the existing backend bearer. The operator password and backend bearer are separate credentials.

### 2.3 Connect to a remote VPS backend

Do not expose backend port 8090 publicly. Open an SSH tunnel from the desktop and keep it running:

```bash
ssh -N -L 18090:127.0.0.1:8090 YOUR_SSH_USER@YOUR_VPS_HOST
```

Enter `http://127.0.0.1:18090` as the runtime endpoint. The tunnel must remain open while using the console. SSH credentials belong in your terminal/SSH agent, never in the desktop application.

### 2.4 Sample workspace versus Runtime view

- **Sample workspace** contains illustrative sample data. It is not live or paper account evidence.
- **Runtime view** reads the authenticated backend. An unattached backend returns empty/unavailable financial state; it must not silently substitute sample positions.
- A failed or stale runtime snapshot clears financial views rather than retaining old positions as if they were current.

## 3. Workspace pages

| Page | Purpose | Important limitations |
|---|---|---|
| Overview | High-level runtime status, position summary and pipeline/module observations | A healthy API response is not a production-readiness or trading-profitability certificate |
| Portfolio | Current position and exposure context | Does not independently calculate full market-price mark-to-market equity unless the backend supplies it |
| Trade Journal | Closed/open lifecycle records, search, direction filtering and CSV export | Journal pagination is offset-based; new rows can shift pages. Totals cover loaded rows, not all-time account P&L |
| Research Lab | Research workspace and evaluation-planning context | The Evaluation Planner does not itself execute a backtest; the separate StrategyEngine is proposal/allocation-only until a typed bridge through TradeLifecycle is implemented |
| Observability | Pipeline stage latency and operational signals | Unmeasured p50/p95/p99 values remain unavailable; they are not zero |
| Connections | Runtime and adapter connection context | Connection display does not store exchange keys or activate an account |
| Configuration | Local configuration drafts and export | Drafts are not applied to the backend or trading runtime |
| Quality & Readiness | Evidence-backed gates, status counts and next actions | A gate count or page verdict is not a production certification; live authorization remains false |
| Performance | Closed-ledger performance and cost attribution | Uses a bounded recent ledger window; not a complete account-equity curve or mark-price valuation |
| Persistence & Recovery | Persistence and recovery observations | The UI does not repair journals, clear quarantine or release capital |

### Common interactions

- Use the workspace navigation to switch pages.
- Search and direction filters narrow the loaded Trade Journal rows.
- Use Previous/Next to move between available journal batches.
- Export the currently loaded/filtered journal data as CSV.
- Select latency percentiles in Observability; absent samples remain unavailable.
- Save configuration drafts locally or export the draft JSON. Saving a draft does not mutate a running trading process.
- Expand a Quality & Readiness gate to inspect its evidence and next action.

## 4. Credential and security boundaries

- The operator console is read-only. It does not place, modify or cancel exchange orders.
- Never put an exchange API key/secret in the operator workspace, browser storage, Vite environment variables or chat.
- The Tauri Trusted Signer Settings window is separate from the operator workspace and currently supports testnet credential provisioning through a local credential provider. Use it only on a trusted, user-controlled device.
- A trusted-host acknowledgement is an operator warning, not technical proof that the host is trusted.
- Do not provision or unlock exchange credentials on a provider-managed VPS/RDP if its administrator is outside your trust boundary.
- Paper-only signer/testnet components do not authorize production remote signing or live order dispatch.
- Keep the Vite development preview unattached. For approved runtime reads, use the authenticated operator gateway.

## 5. Understanding unavailable or stale data

The console validates backend response schemas and timestamps. A stale snapshot, invalid timestamp, malformed response or gateway error should result in the financial view being cleared or marked unavailable. Do not interpret unavailable values as zero, and do not infer missing P&L or equity.

Some views intentionally have bounded scope:

- Journal pages and totals may represent only the loaded records.
- Performance uses a bounded recent ledger-update window.
- Runtime position P&L is a lifecycle record unless a separate mark-price valuation is explicitly supplied.
- Pipeline latency is not measured until actual samples are observed.

## 6. Troubleshooting

| Symptom | What to check |
|---|---|
| Vault unavailable | Unlock Windows Credential Manager / Linux Secret Service; confirm the user session's keyring is available |
| Sign-in rejected | Confirm the local operator password; this is separate from the backend bearer |
| Backend connection fails | Confirm backend health, exact origin/port, `AITOS_CONSOLE_TOKEN` and bearer value |
| VPS connection fails | Keep the SSH tunnel running and use `http://127.0.0.1:18090`; do not expose 8090 publicly |
| Workspace is empty | Switch to Runtime view and confirm an approved backend is attached; an unattached adapter has no real financial data |
| Snapshot disappears | Check freshness, backend health and gateway errors; stale data is intentionally cleared |
| Configuration change has no effect | Configuration entries are local drafts/export only; they are not applied to the trading runtime |

For platform installation, package checksums, uninstall behavior and detailed backend setup, see [INSTALLATION.md](../INSTALLATION.md). For gateway security and authentication details, see [Operator Gateway](OPERATOR_GATEWAY.md).

## 7. Strategy and research boundary

The StrategyEngine can evaluate strategy families and allocate a research capital budget, but it is not an end-to-end order router. Its results must pass through the canonical Opportunity and TradeLifecycle boundary before any execution adapter can be considered. Do not interpret a StrategyEngine result, allocation, or Research Lab display as an approved order or live position.

## 8. What the desktop application does not certify

The desktop UI does not certify exchange connectivity, account reconciliation, execution correctness, paper-soak stability, profitability, external TLS, trusted code signing, production remote signing, live-trading readiness or disaster recovery. Those require separate source, CI, runtime and operational acceptance evidence.
