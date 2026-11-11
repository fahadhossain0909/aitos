#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 DNS:signer.example.com|IP:10.0.0.10 spiffe://aitos/runtime/paper" >&2
  exit 2
fi
server_san="$1"
client_identity="$2"
case "$server_san" in
  DNS:*|IP:*) ;;
  *) echo "Server SAN must be DNS:... or IP:..." >&2; exit 2 ;;
esac
if [[ ! "$client_identity" =~ ^spiffe://[A-Za-z0-9._/-]{1,200}$ ]]; then
  echo "Invalid SPIFFE client identity" >&2
  exit 2
fi
if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required" >&2
  exit 2
fi

cert_dir="\${AITOS_SIGNER_CERT_DIR:-\${HOME}/.config/aitos-signer}"
install -d -m 700 "$cert_dir"
ca_key="\${cert_dir}/client-ca.key"
ca_cert="\${cert_dir}/client-ca.crt"
server_key="\${cert_dir}/server.key"
server_cert="\${cert_dir}/server.crt"

if [[ -e "$ca_key" || -e "$ca_cert" ]]; then
  if [[ ! -f "$ca_key" || ! -f "$ca_cert" || -L "$ca_key" || -L "$ca_cert" ]]; then
    echo "Incomplete or symlinked CA material; refusing to overwrite" >&2
    exit 2
  fi
else
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$ca_key"
  openssl req -x509 -new -sha256 -key "$ca_key" -days 3650 \
    -subj "/CN=AITOS User-Controlled Signer CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -out "$ca_cert"
  chmod 600 "$ca_key"
fi

if [[ ! -e "$server_key" && ! -e "$server_cert" ]]; then
  server_csr="\${cert_dir}/server.csr"
  server_ext="\${cert_dir}/server.ext"
  openssl req -new -newkey rsa:3072 -nodes -keyout "$server_key" \
    -out "$server_csr" -subj "/CN=AITOS Signer"
  cat > "$server_ext" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=\${server_san}
EOF
  openssl x509 -req -sha256 -in "$server_csr" -CA "$ca_cert" \
    -CAkey "$ca_key" -CAcreateserial -days 365 -extfile "$server_ext" \
    -out "$server_cert"
  rm -f "$server_csr" "$server_ext"
  chmod 600 "$server_key"
elif [[ ! -f "$server_key" || ! -f "$server_cert" || -L "$server_key" || -L "$server_cert" ]]; then
  echo "Incomplete or symlinked server TLS material; refusing to overwrite" >&2
  exit 2
fi

client_key_tmp="$(mktemp "\${cert_dir}/client.XXXXXX.key")"
client_csr_tmp="$(mktemp "\${cert_dir}/client.XXXXXX.csr")"
client_cert_tmp="$(mktemp "\${cert_dir}/client.XXXXXX.crt")"
client_ext_tmp="$(mktemp "\${cert_dir}/client.XXXXXX.ext")"
cleanup() {
  rm -f "$client_key_tmp" "$client_csr_tmp" "$client_cert_tmp" "$client_ext_tmp"
}
trap cleanup EXIT

openssl req -new -newkey rsa:3072 -nodes -keyout "$client_key_tmp" \
  -out "$client_csr_tmp" -subj "/CN=\${client_identity}"
cat > "$client_ext_tmp" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=clientAuth
subjectAltName=URI:\${client_identity}
EOF
openssl x509 -req -sha256 -in "$client_csr_tmp" -CA "$ca_cert" \
  -CAkey "$ca_key" -CAserial "\${cert_dir}/client-ca.srl" \
  -days 365 -extfile "$client_ext_tmp" -out "$client_cert_tmp"
serial="$(openssl x509 -in "$client_cert_tmp" -noout -serial | cut -d= -f2 | tr '[:lower:]' '[:upper:]')"
client_key="\${cert_dir}/client-\${serial}.key"
client_cert="\${cert_dir}/client-\${serial}.crt"
if [[ -e "$client_key" || -e "$client_cert" ]]; then
  echo "Certificate serial output already exists; refusing to overwrite" >&2
  exit 2
fi
mv "$client_key_tmp" "$client_key"
mv "$client_cert_tmp" "$client_cert"
chmod 600 "$client_key" "$ca_key" "$server_key"
chmod 644 "$ca_cert" "$server_cert" "$client_cert"

not_before="$(openssl x509 -in "$client_cert" -noout -startdate | cut -d= -f2)"
not_after="$(openssl x509 -in "$client_cert" -noout -enddate | cut -d= -f2)"
valid_from_ms="$(date -u -d "$not_before" +%s)000"
valid_until_ms="$(date -u -d "$not_after" +%s)000"
now_ms="$(date +%s%3N)"

openssl verify -CAfile "$ca_cert" "$server_cert" "$client_cert" >/dev/null
cat <<EOF
Signer PKI material created/verified.

Signer host files:
  Server certificate: \${server_cert}
  Server private key: \${server_key} (0600)
  Client CA certificate: \${ca_cert}
  CA private key: \${ca_key} (0600; never copy off this trusted host)

Copy only the client certificate and client private key to the AITOS runtime host:
  Client certificate: \${client_cert}
  Client private key: \${client_key} (0600)

Enroll this client certificate in the persistent signer registry:
  python -m aitos.security.signer_cli enroll-client \\
    --identity \${client_identity} \\
    --account-alias paper-primary \\
    --symbols BTCUSDT \\
    --serial \${serial} \\
    --valid-from-ms \${valid_from_ms} \\
    --valid-until-ms \${valid_until_ms} \\
    --now-ms \${now_ms}

For rotation, rerun with the same identity; a new serial is enrolled with its own validity window.
Revoke the old serial after the new client certificate has been installed and verified.
EOF
