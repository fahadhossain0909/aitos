"""Explicit standalone CI acceptance, never collected as a mocked vault test."""

import hashlib
import os
import secrets
import subprocess
import sys
import uuid

from aitos.native.credential_vault import get_credential_vault


def main():
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise SystemExit("Real vault acceptance is restricted to disposable CI")
    vault = get_credential_vault()
    slot = "synthetic-ci/" + uuid.uuid4().hex
    secret = secrets.token_urlsafe(32)
    try:
        vault.put(slot, secret)
        assert vault.get(slot) == secret
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import hashlib,sys; from aitos.native.credential_vault import get_credential_vault; value=get_credential_vault().get(sys.argv[1]); print(hashlib.sha256(value.encode()).hexdigest())",
                slot,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert child.stdout.strip() == hashlib.sha256(secret.encode()).hexdigest()
        vault.put(slot, secret + "replacement")
        assert vault.get(slot) == secret + "replacement"
        assert vault.delete(slot) and vault.get(slot) is None
        assert not vault.delete(slot)
    finally:
        vault.delete(slot)
    print(
        "Real OS vault write/read/replace/cross-process persistence/delete PASS; synthetic only"
    )


if __name__ == "__main__":
    main()
