# AITOS Installation & Deployment Guide

This guide documents the two supported ways to use AITOS:

1. **Desktop Operator Console** — install the Windows or Linux desktop package on your computer and connect it to an already-running AITOS backend.
2. **VPS Runtime Deployment** — run the AITOS trading/data services on a Linux VPS using the existing GitHub Actions + Docker Compose deployment pipeline.

These are different layers, not competing installers. The desktop application is an operator interface; it does **not** install, host, or start the trading engine. The VPS method deploys the backend runtime. You can use either method independently, or install the desktop console and connect it to a VPS runtime.

> **Safety:** the desktop console is read-only. The standard VPS deployment starts the paper-trading runtime using public market data and simulated orders. It does not authorize live trading. Do not expose database ports or the backend health/API port directly to the public Internet.

> **Credential warning for provider-hosted RDP/VPS:** do not enter an exchange API Secret on an RDP/VPS if the hosting provider's administrators are outside your trust boundary. Windows Credential Manager or Linux Secret Service does not protect a secret from a privileged administrator of that same host. The current desktop app stores only its operator password/backend bearer; it has no exchange-key entry flow. Existing live execution configuration that places `BINANCE_API_KEY` / `BINANCE_API_SECRET` in the VPS environment is visible to sufficiently privileged host administrators. Review the [Credential Security & Remote Signing RFC](docs/CREDENTIAL_SECURITY_REMOTE_SIGNING_RFC.md); the separate remote signer is not yet implemented.

## Which method should I choose?

| Goal | Choose |
|---|---|
| Monitor an existing local AITOS runtime from a desktop | Desktop installation |
| Run AITOS continuously without keeping your PC on | VPS deployment |
| Use a desktop UI while AITOS runs remotely | VPS deployment + desktop installation + SSH tunnel |
| Run the trading engine on the desktop itself | Use the local development/runtime instructions; the desktop installer alone does not do this |

---

# Method 1 — Install the Desktop Operator Console

## 1.1 What the desktop app does

The native desktop package installs the AITOS read-only Operator Console session manager. It opens the bundled web interface in your default browser and runs a small authenticated gateway on loopback.

It does not:
- install Docker, Redis, ClickHouse or Neo4j;
- start `run_paper_trading.py` or `run_live_trading.py`;
- place, modify or cancel exchange orders;
- store Binance exchange API keys;
- provide a signed/trusted production release or automatic updater.

A backend must already be running and have its console endpoint enabled. The desktop app stores the operator password and backend bearer token in the operating system credential vault; it does not use a plaintext-file fallback.

## 1.2 Supported desktop packages

| Platform | Package | Current status |
|---|---|---|
| Windows 10/11 x64 | `AITOS-Console-0.1.0-Windows-x64.exe` (Inno Setup installer) | Unsigned test installer |
| Ubuntu 24.04 / Debian-family x64 | `aitos-console_0.1.0_amd64.deb` | Unsigned test package |
| Debian-family ARM64 | `aitos-console_0.1.0_arm64.deb` | Build target when Python 3.12 and dependencies are available |
| macOS | — | No supported installer yet |

The Windows and Linux installers are native session managers, not the experimental Tauri shell. Do not assume a macOS build, signed release, updater, or one-click backend installation is available.

## 1.3 Get the installer

There are two ways to obtain a package:

**Option A — Download a validated CI artifact**

1. Open the [AITOS repository](https://github.com/fahadhossain0909/aitos/actions).
2. Open the latest successful **Windows console build and vault verification** workflow for Windows, or **Linux console build and package validation** workflow for Linux.
3. Confirm that the run belongs to the commit you intend to install.
4. In **Artifacts**, download:
   - Windows: `UNSIGNED-Windows-console-test-only`
   - Linux: `unsigned-linux-console-deb`
5. Extract the downloaded artifact ZIP. Keep the accompanying checksum file where supplied.
6. Treat these as unsigned test artifacts. Verify their source commit and checksum before installation; do not bypass SmartScreen/Defender or OS security warnings.

A workflow artifact is not a GitHub Release and may expire according to repository artifact-retention settings.

**Option B — Build from source**

Prerequisites:
- Python 3.12
- Node.js 22.12 or later
- Git
- AITOS repository checkout

Clone the repository:

```bash
git clone https://github.com/fahadhossain0909/aitos.git
cd aitos
```

Windows additionally requires Inno Setup 6.3+ at its standard installation path. In PowerShell, from the repository root:

```powershell
./native/windows/build.ps1
```

The installer is written under `artifacts/windows/`.

Linux requires an Ubuntu 24.04/Debian-family build machine with Tk, dpkg tooling and Secret Service development/runtime libraries. Install the build dependencies, then run:

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv python3-tk dpkg-dev \
  libsecret-1-0 libglib2.0-0 libdbus-1-3 libnss3 \
  libx11-6 libxext6 libxrender1 libxss1 libxtst6
bash native/linux/build.sh
```

The Debian package and `SHA256SUMS` are written under `artifacts/linux/`. Build and target Python major/minor versions must match.

## 1.4 Install on Windows

1. Download and extract the Windows CI artifact, or build it using section 1.3.
2. Locate `AITOS-Console-0.1.0-Windows-x64.exe`.
3. Verify that it is the expected unsigned test artifact from the intended commit.
4. Run the installer. It installs per-user under your Windows profile; administrator privileges are not required.
5. Leave **Open AITOS read-only console** selected if you want to launch it immediately.
6. On first launch:
   - Create a strong operator password and confirm it.
   - Enter the exact backend origin, for example `http://127.0.0.1:8090` for a local backend.
   - Enter the backend bearer token when prompted.
7. The app saves the operator password and backend token in Windows Credential Manager.
8. The authenticated console opens in your default browser. Use the operator password to sign in.
9. To close the session manager and revoke its local sessions, use **Close console and revoke sessions**.

If the Windows credential vault is unavailable, setup fails closed. There is no plaintext fallback. Uninstalling the app deliberately preserves credential entries; remove them separately from Windows Credential Manager if you no longer need them.

## 1.5 Install on Linux

Supported target: Ubuntu 24.04 / Debian-family desktop with Python 3.12, Tk and an unlocked Secret Service provider (GNOME Keyring or compatible KWallet).

1. Download and extract the Linux CI artifact, or build the package using section 1.3.
2. Open a terminal in the directory containing the `.deb` file.
3. Verify the checksum:

   ```bash
   sha256sum --check SHA256SUMS
   ```

4. Install the package:

   ```bash
   sudo apt install ./aitos-console_0.1.0_amd64.deb
   ```

   Use the actual filename/architecture downloaded.

5. Launch **AITOS Console** from the desktop application menu, or run:

   ```bash
   aitos-console
   ```

6. On first launch, create and confirm an operator password.
7. Enter the backend origin and bearer token when prompted.
8. The credentials are stored through the desktop session's Secret Service keyring. If D-Bus/keyring is unavailable or locked, startup fails closed; do not configure a plaintext backend.
9. The authenticated console opens in your default browser.

Uninstall:

```bash
sudo apt remove aitos-console
```

Uninstall does not remove your keyring credentials. Remove them through your desktop keyring manager if required.

## 1.6 Connect the desktop app to a local backend

The desktop app does not start the backend. Start an already-configured AITOS paper runtime separately and verify its health endpoint:

```bash
curl http://127.0.0.1:8090/health
```

The backend must have `AITOS_CONSOLE_TOKEN` configured with a strong bearer token (at least 32 characters) before its authenticated console routes are enabled. Enter that token in the desktop app when prompted. Do not put it in a `VITE_` variable, browser storage, source code or chat.

For local development and unattached/sample mode, see [Local development](docs/LOCAL_DEVELOPMENT.md) and [Operator gateway](docs/OPERATOR_GATEWAY.md). An unattached backend will not display real account or trading evidence.

## 1.7 Connect the desktop app to a VPS backend

Use an SSH tunnel rather than exposing port 8090 publicly.

1. Ensure the VPS backend is configured with `AITOS_CONSOLE_TOKEN` and its console routes are enabled. Add the token as the GitHub Actions `papertrade` environment secret named `AITOS_CONSOLE_TOKEN` so the CD workflow can write it into the runtime environment on deployment.
2. From your desktop, open a terminal and keep this SSH tunnel running:

   ```bash
   ssh -N -L 18090:127.0.0.1:8090 YOUR_SSH_USER@YOUR_VPS_HOST
   ```

3. In the desktop app's Runtime endpoint prompt, enter:

   ```text
   http://127.0.0.1:18090
   ```

4. Enter the same backend bearer token configured in the VPS environment.
5. Sign in using the separate operator password you created locally.

The SSH tunnel must remain open while using the console. Closing it interrupts remote reads. Never enter the VPS SSH password/key into the desktop app; SSH authentication belongs to your terminal/SSH agent.

---

# Method 2 — Deploy the AITOS Runtime on a VPS

## 2.1 Deployment architecture

The existing production deployment is automated through GitHub Actions and SSH:

```text
GitHub push / manual workflow dispatch
             |
             v
      CI / CD workflow
             |
             +--> Build immutable Docker image
             |    ghcr.io/fahadhossain0909/aitos:sha-<commit>
             |
             +--> SSH to the configured VPS
                       |
                       v
              /home/<deploy-user>/aitos
                       |
                Docker Compose stack
                 |       |       |
               Redis  ClickHouse Neo4j
                         |
                    AITOS paper runtime
                    + learning/maintenance
                         |
                 Persistent data disk
                    /mnt/aitos-data
```

The deployment workflow is the canonical production path. It builds and publishes the image, downloads the exact source commit to the VPS, checks and migrates the configured data layout, writes a protected runtime `.env`, pulls the immutable image and recreates the Compose stack. It then waits for Redis, ClickHouse and Neo4j health and verifies persistent storage paths.

The default runtime is paper trading. The live-trading Compose service is profile-gated and is not started by the standard deployment.

## 2.2 VPS prerequisites

Prepare a dedicated Linux VPS with:

- A supported Linux distribution and SSH access.
- Docker Engine and Docker Compose v2 installed.
- `sudo` access for storage mount/ownership preparation.
- `flock`, `curl`, `tar`, `mountpoint`, `findmnt`, `lsblk` and standard GNU utilities.
- A dedicated persistent data disk, mounted at `/mnt/aitos-data`.
- Enough CPU, RAM and disk capacity for Redis, ClickHouse, Neo4j and the AITOS services. Size this for the expected market-data retention and research workload; do not assume a small/free instance is sufficient.
- SSH public-key authentication from GitHub Actions.

The production deployment uses:
- Application checkout: `$HOME/aitos`
- Persistent data root: `/mnt/aitos-data`
- Docker Compose project: `aitos`
- GitHub Actions environment: `papertrade`

## 2.3 Prepare the persistent data disk

**Do not format an existing disk.** Formatting destroys data. Device names differ across VPS providers.

1. Identify disks:

   ```bash
   lsblk -o NAME,SIZE,FSTYPE,TYPE,MOUNTPOINTS,UUID
   ```

2. Identify the intended dedicated data disk and record its filesystem UUID:

   ```bash
   sudo blkid
   ```

3. Ensure that disk is mounted at `/mnt/aitos-data` and that the mount persists across reboot using its UUID in `/etc/fstab`. Do not copy an example device name without checking it on your host.
4. Verify:

   ```bash
   mountpoint /mnt/aitos-data
   findmnt --target /mnt/aitos-data
   df -hT /mnt/aitos-data
   ```

5. Record the exact filesystem UUID. The deployment uses it as a safety check and fails closed if the configured UUID does not match the mounted device.

The CD workflow runs the legacy-storage migration before its storage bootstrap. Therefore the data disk must already be mounted at `/mnt/aitos-data` before the first deployment. Existing data must be reviewed and migrated deliberately; do not run hard-reset or formatting workflows as part of ordinary installation.

## 2.4 Prepare SSH access

1. Create a dedicated deployment user on the VPS.
2. Install the GitHub Actions public key in that user's `~/.ssh/authorized_keys`.
3. Grant only the host permissions required for Docker and the storage bootstrap/deployment commands.
4. Confirm that GitHub Actions can connect using the private key.
5. Do not commit the private key or paste it into chat.

The workflow serializes deployments with a host-side lock to avoid overlapping cleanup/deploy operations.

## 2.5 Configure GitHub Actions

In the GitHub repository, configure:

**Repository variable**

- `AITOS_VPS_ENABLED=true`

**GitHub Actions environment**

Create or use an environment named `papertrade`. Add these secrets:

| Secret | Required | Purpose |
|---|---:|---|
| `DEPLOY_HOST` | Yes | VPS hostname/IP |
| `DEPLOY_USER` | Yes | SSH deployment user |
| `DEPLOY_SSH_KEY` | Yes | Private SSH key for Actions |
| `AITOS_DATA_DISK_UUID` | Yes | UUID of the mounted persistent data disk |
| `REDIS_PASSWORD` | Yes | Redis authentication |
| `CLICKHOUSE_PASSWORD` | Yes | ClickHouse authentication |
| `NEO4J_PASSWORD` | Yes | Neo4j authentication |
| `AITOS_CONSOLE_TOKEN` | For desktop remote console | Strong backend bearer token, minimum 32 characters |
| `CLICKHOUSE_USER` | Optional | Defaults to `default` |
| `NEO4J_USER` | Optional | Defaults to `neo4j` |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Optional | Not needed for public-market-data paper trading |
| `BINANCE_TESTNET` | Optional | Defaults to `true` |
| `DATABASE_URL`, `SENTRY_DSN`, `SLACK_WEBHOOK_URL` | Optional | External integrations |

Use unique strong passwords. Do not reuse staging credentials or production secrets without an explicit isolation/security decision. Never grant withdrawal permission to exchange keys.

## 2.6 Trigger the deployment

The current production CD workflow runs on pushes to `main` / `master`, version tags matching `v*`, or manual workflow dispatch.

1. Ensure the intended code is merged to an allowed deployment ref, or deliberately select the intended ref for manual dispatch.
2. Open **GitHub → Actions → CD**.
3. Select **Run workflow** if using manual dispatch and choose the intended branch/ref.
4. Start the workflow.
5. Monitor the `publish-image` and `deploy` jobs.
6. Confirm that the deploy job completes its health and storage gates.

The workflow will not deploy when repository variable `AITOS_VPS_ENABLED` is not exactly `true`, or when required secrets are missing.

## 2.7 Verify the VPS runtime

SSH to the VPS:

```bash
cd ~/aitos
docker compose ps --all
docker compose logs --tail=200 aitos-paper
docker compose logs --tail=100 redis clickhouse neo4j
```

Check the paper runtime health locally on the VPS:

```bash
curl http://127.0.0.1:8090/health
```

Check persistent storage:

```bash
mountpoint /mnt/aitos-data
df -hT /mnt/aitos-data
```

The infrastructure and health ports are bound to loopback by Compose. Keep Redis, ClickHouse, Neo4j, and port 8090 private. For remote operator-console access, use the SSH tunnel in section 1.7.

## 2.8 Update an existing deployment

Push or manually deploy the next approved commit through the same CD workflow. Do not run an ad-hoc source rebuild on the VPS as a substitute for the immutable-image pipeline. Confirm the exact commit SHA and successful health/storage gates in the Actions run.

## 2.9 Stop and restart services

For routine maintenance, use Compose from `~/aitos`:

```bash
docker compose stop aitos-paper
docker compose start aitos-paper
```

To inspect or restart the full stack, first understand the dependency and maintenance impact:

```bash
docker compose ps --all
docker compose restart aitos-paper
```

Do not use `docker compose down -v` during normal maintenance. It can remove named volumes. Do not run hard-reset workflows, manually delete database directories, or remove `/mnt/aitos-data` as routine troubleshooting.

## 2.10 Rollback and recovery

A deployment uses immutable commit-tagged images. If a deployment fails:

1. Stop and inspect; do not repeatedly redeploy blindly.
2. Capture the failed Actions run, commit SHA, Compose state and service logs.
3. Preserve `/mnt/aitos-data` and all database contents.
4. Roll back only through a reviewed commit/image procedure that matches the existing deployment workflow.
5. Do not restore, delete or migrate financial/runtime data as an improvised code rollback.

A successful container health check is not proof of profitable trading, account reconciliation, live-execution readiness or production certification.

---

## Troubleshooting

| Symptom | What to check |
|---|---|
| Desktop says vault unavailable | Windows Credential Manager or unlocked Linux Secret Service/D-Bus |
| Desktop cannot read runtime | Backend is running, correct origin/port, console token enabled, bearer token matches |
| VPS desktop connection fails | SSH tunnel is running; use local tunnel origin `http://127.0.0.1:18090`; port 8090 is not public |
| CD does not start | `AITOS_VPS_ENABLED=true`, correct ref, `papertrade` environment secrets |
| CD fails storage preflight | Data disk is mounted at `/mnt/aitos-data`; configured UUID matches actual mounted disk |
| Containers unhealthy | Inspect `docker compose ps --all` and service logs; preserve data while diagnosing |
| Disk space low | Check `df -hT /mnt/aitos-data`; do not delete database files manually |

## Related documentation

- [Project overview and local quick start](README.md)
- [Desktop console details](desktop/README.md)
- [Operator gateway and security boundary](docs/OPERATOR_GATEWAY.md)
- [Staging deployment](docs/STAGING_DEPLOYMENT.md)
- [Portable desktop and storage engineering notes](engineering/PORTABLE_DESKTOP_AND_STORAGE.md)
