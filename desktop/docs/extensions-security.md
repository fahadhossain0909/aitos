# AITOS desktop extensions: signed package contract

## Security status

The desktop currently has a metadata registry and a cryptographic package verifier. It does **not** execute extension code. A package is not eligible for registration through `registerSignedExtension` unless its manifest and every packaged file verify against an explicitly trusted Ed25519 publisher key.

The trust policy must be supplied by a future trusted publisher-management layer. There is deliberately no permissive built-in publisher key, and an empty trust map rejects every package. Do not put a publisher private key in AITOS or in the desktop application.

## Package envelope

A package is a JSON object with exactly these fields:

- `manifest`: A valid AITOS extension manifest (API version 1).
- `files`: A map of relative package paths to canonical Base64 file bytes.
- `signerKeyId`: A stable publisher key identifier.
- `signature`: Base64 Ed25519 signature.

The signed payload is the UTF-8 encoding of canonical JSON (recursively sorted object keys) containing:

- `apiVersion: 1`
- the normalized manifest
- `files`: a path-to-lowercase-SHA-256 digest map, sorted by path
- `signerKeyId`

The Ed25519 signature is checked against a raw 32-byte public key supplied by the trusted publisher policy. The signature binds the normalized manifest, requested capabilities, entrypoint, publisher ID and every packaged file digest.

## Enforced limits

- 128 files maximum.
- 2 MiB maximum per file.
- 5 MiB maximum total uncompressed file bytes.
- 240-character maximum relative path.
- Only package-relative paths with a restricted ASCII path alphabet and supported file extensions.
- Absolute paths, dot segments, backslashes, NULs, malformed Base64, missing entrypoints, unknown publishers, invalid key/signature sizes and tampered content are rejected.
- Caller-supplied size limits may lower, but cannot raise, the hard limits.
- Registration remains disabled and unapproved by default; cryptographic verification is not user approval.

## Publisher onboarding requirements (not implemented)

Before any publisher can be trusted, a separate settings flow must support out-of-band fingerprint confirmation, key rotation, revocation, durable audit history and recovery. Do not trust a key merely because it appears in a package.

## Runtime isolation requirements (not implemented)

Signature verification is not a sandbox. Do not evaluate extension entrypoints in the operator WebView, signer WebView, main application process or unrestricted Web Worker. A production extension host still needs a separately sandboxed process with OS-enforced network/filesystem/CPU/memory limits, a narrow capability RPC protocol, crash containment, revocation and security testing on Windows, Linux and macOS. Until then, package code must remain inert.
