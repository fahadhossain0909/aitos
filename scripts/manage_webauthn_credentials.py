"""Offline WebAuthn authenticator inventory and recovery utility.

Run only from a trusted host with filesystem access to the gateway credential DB.
This utility is intentionally not exposed as an HTTP or desktop credential-delete
endpoint.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from aitos.security.webauthn_rp import WebAuthnRelyingParty


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--rp-id", required=True)
    parser.add_argument("--origin", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list non-secret authenticator metadata")
    revoke = commands.add_parser("revoke", help="revoke one credential by ID")
    revoke.add_argument("--credential-id", required=True)
    revoke.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    rp = WebAuthnRelyingParty(
        args.database,
        rp_id=args.rp_id,
        rp_name="AITOS Operator Console",
        allowed_origins={args.origin},
    )
    if args.command == "list":
        print(json.dumps(rp.list_credentials(user_id=args.user_id), indent=2))
        return 0
    if not args.confirm:
        parser.error("credential revocation requires --confirm")
    changed = rp.revoke_credential(
        user_id=args.user_id, credential_id=args.credential_id
    )
    if not changed:
        parser.error("credential not found for the requested identity")
    print(json.dumps({"revoked": True, "user_id": args.user_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
