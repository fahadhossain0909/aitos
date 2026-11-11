#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "\${BASH_SOURCE[0]}")/.." && pwd)"
venv="\${repo_root}/.venv-signer"
config_dir="\${HOME}/.config/aitos-signer"
data_dir="\${HOME}/.local/share/aitos-signer"
unit_dir="\${HOME}/.config/systemd/user"
env_file="\${config_dir}/signer.env"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 2
fi
if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd user services are required for this installer" >&2
  exit 2
fi

python3 -m venv "\${venv}"
"\${venv}/bin/python" -m pip install --upgrade pip keyring
install -d -m 700 "\${config_dir}" "\${data_dir}" "\${unit_dir}"

if [[ ! -f "\${env_file}" ]]; then
  cat > "\${env_file}" <<EOF
AITOS_SIGNER_DATA_DIR=\${data_dir}
AITOS_SIGNER_ACCOUNT_ALIAS=paper-primary
AITOS_SIGNER_ALLOWED_SYMBOLS=BTCUSDT
AITOS_SIGNER_BIND_HOST=127.0.0.1
AITOS_SIGNER_PORT=9443
AITOS_SIGNER_SERVER_CERT=\${config_dir}/server.crt
AITOS_SIGNER_SERVER_KEY=\${config_dir}/server.key
AITOS_SIGNER_CLIENT_CA=\${config_dir}/client-ca.crt
AITOS_SIGNER_MAX_ORDER_NOTIONAL=100
AITOS_SIGNER_MAX_GROSS_EXPOSURE=1000
AITOS_SIGNER_MAX_SYMBOL_EXPOSURE=500
AITOS_SIGNER_MAX_DAILY_LOSS=50
AITOS_SIGNER_MAX_ORDERS_PER_WINDOW=5
AITOS_SIGNER_RPC_RATE_LIMIT=10
AITOS_SIGNER_RPC_RATE_WINDOW_MS=60000
AITOS_SIGNER_REDUCE_ONLY_RPC_RATE_LIMIT=100
EOF
  chmod 600 "\${env_file}"
fi

sed \
  -e "s|@PYTHON@|\${venv}/bin/python|g" \
  -e "s|@REPO_ROOT@|\${repo_root}|g" \
  -e "s|@ENV_FILE@|\${env_file}|g" \
  -e "s|@DATA_DIR@|\${data_dir}|g" \
  "\${repo_root}/deploy/signer/aitos-signer.service.in" \
  > "\${unit_dir}/aitos-signer.service"
chmod 600 "\${unit_dir}/aitos-signer.service"
systemctl --user daemon-reload

cat <<EOF
Signer runtime prepared for this user-controlled Linux host.

Next:
1. Review \${env_file}; keep loopback binding unless a private VPN/network path is configured.
2. Place the CA-issued server certificate, server private key (mode 0600), and client CA certificate at the configured paths.
3. Store the Binance Futures Testnet credential:
   PYTHONPATH="\${repo_root}" "\${venv}/bin/python" -m aitos.security.signer_cli store-testnet-credential
4. Enroll a CA-issued AITOS client certificate:
   PYTHONPATH="\${repo_root}" "\${venv}/bin/python" -m aitos.security.signer_cli enroll-client --identity spiffe://aitos/runtime/paper --account-alias paper-primary --symbols BTCUSDT --serial CERT_SERIAL_HEX --valid-from-ms START_MS --valid-until-ms END_MS --now-ms NOW_MS
5. Enable only after the certificate files, credential vault, and network firewall are verified:
   systemctl --user enable --now aitos-signer.service

This installer does not issue certificates, copy exchange credentials from another host,
start the service, open firewall ports, or enable live trading.
EOF
