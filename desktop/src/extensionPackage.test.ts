import { Buffer } from "node:buffer";
import { webcrypto } from "node:crypto";
import { beforeAll, describe, expect, it } from "vitest";
import {
  extensionSignaturePayload,
  registerSignedExtension,
  verifySignedExtensionPackage,
} from "./extensionPackage";

const manifest = {
  apiVersion: 1,
  id: "aitos.example.indicator",
  name: "Example Indicator",
  version: "1.2.0",
  description: "A signed example indicator extension.",
  entrypoint: "dist/index.mjs",
  capabilities: ["market-data.read", "workspace.panel"],
};
const signerKeyId = "aitos.publisher.test";
const source = new TextEncoder().encode("export const activate = () => undefined;");
const files = { "dist/index.mjs": Buffer.from(source).toString("base64") };

let publicKey: string;
let privateKey: CryptoKey;

beforeAll(async () => {
  Object.defineProperty(globalThis, "crypto", { configurable: true, value: webcrypto });
  const pair = await webcrypto.subtle.generateKey(
    { name: "Ed25519" } as AlgorithmIdentifier,
    true,
    ["sign", "verify"],
  ) as CryptoKeyPair;
  privateKey = pair.privateKey;
  publicKey = Buffer.from(await webcrypto.subtle.exportKey("raw", pair.publicKey)).toString("base64");
});

async function signedPackage(overrides: Record<string, unknown> = {}) {
  const payload = await extensionSignaturePayload(manifest, files, signerKeyId);
  const signature = await webcrypto.subtle.sign(
    { name: "Ed25519" } as AlgorithmIdentifier,
    privateKey,
    payload,
  );
  return {
    manifest,
    files,
    signerKeyId,
    signature: Buffer.from(signature).toString("base64"),
    ...overrides,
  };
}

describe("signed extension package verification", () => {
  it("verifies a trusted Ed25519 publisher and returns bounded package bytes", async () => {
    const result = await verifySignedExtensionPackage(await signedPackage(), {
      trustedEd25519Keys: { [signerKeyId]: publicKey },
    });
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.manifest.id).toBe(manifest.id);
      expect(result.files.get("dist/index.mjs")).toEqual(source);
      expect(result.signerKeyId).toBe(signerKeyId);
    }
  });

  it("rejects an untrusted publisher even when the package is correctly signed", async () => {
    const result = await verifySignedExtensionPackage(await signedPackage(), {
      trustedEd25519Keys: {},
    });
    expect(result).toEqual({ ok: false, error: "publisher_not_trusted" });
  });

  it("rejects tampered file bytes and manifest changes", async () => {
    const pkg = await signedPackage() as Record<string, any>;
    const tampered = {
      ...pkg,
      files: { ...pkg.files, "dist/index.mjs": Buffer.from("malicious").toString("base64") },
    };
    const badSignature = await verifySignedExtensionPackage(tampered, {
      trustedEd25519Keys: { [signerKeyId]: publicKey },
    });
    expect(badSignature).toEqual({ ok: false, error: "signature_verification_failed" });
    const changedManifest = await verifySignedExtensionPackage({
      ...pkg,
      manifest: { ...manifest, name: "Impersonated publisher" },
    }, { trustedEd25519Keys: { [signerKeyId]: publicKey } });
    expect(changedManifest).toEqual({ ok: false, error: "signature_verification_failed" });
  });

  it("rejects traversal paths, invalid encodings and missing entrypoints", async () => {
    const pkg = await signedPackage() as Record<string, any>;
    const traversal = await verifySignedExtensionPackage({
      ...pkg,
      files: {
        "dist/index.mjs": files["dist/index.mjs"],
        "../payload.js": files["dist/index.mjs"],
      },
    }, { trustedEd25519Keys: { [signerKeyId]: publicKey } });
    expect(traversal).toEqual({ ok: false, error: "package_path_or_encoding_invalid" });
    const malformed = await verifySignedExtensionPackage({
      ...pkg,
      files: { "dist/index.mjs": "not base64!" },
    }, { trustedEd25519Keys: { [signerKeyId]: publicKey } });
    expect(malformed).toEqual({ ok: false, error: "package_file_size_invalid" });
  });

  it("enforces per-file and total package byte limits before signature verification", async () => {
    const pkg = await signedPackage() as Record<string, any>;
    const oversized = await verifySignedExtensionPackage(pkg, {
      trustedEd25519Keys: { [signerKeyId]: publicKey },
      maxFileBytes: 8,
    });
    expect(oversized).toEqual({ ok: false, error: "package_file_size_invalid" });
  });

  it("only registers a signed package after publisher verification", async () => {
    const validPackage = await signedPackage();
    const rejected = await registerSignedExtension(new Map(), validPackage, {
      trustedEd25519Keys: {},
    });
    expect(rejected.registration.ok).toBe(false);
    expect(rejected.registry.size).toBe(0);
    const accepted = await registerSignedExtension(new Map(), validPackage, {
      trustedEd25519Keys: { [signerKeyId]: publicKey },
    });
    expect(accepted.registration.ok).toBe(true);
    expect(accepted.registry.get(manifest.id)).toMatchObject({
      approved: false,
      enabled: false,
    });
  });

  it("rejects prototype-inherited publisher keys and invalid limits", async () => {
    const pkg = await signedPackage() as Record<string, unknown>;
    const inherited = await verifySignedExtensionPackage(pkg, {
      trustedEd25519Keys: Object.create({ [signerKeyId]: publicKey }) as Record<string, string>,
    });
    expect(inherited).toEqual({ ok: false, error: "publisher_not_trusted" });
    const invalidLimit = await verifySignedExtensionPackage(pkg, {
      trustedEd25519Keys: { [signerKeyId]: publicKey },
      maxFileBytes: -1,
    });
    expect(invalidLimit).toEqual({ ok: false, error: "package_policy_invalid" });
    const raisedLimit = await verifySignedExtensionPackage(pkg, {
      trustedEd25519Keys: { [signerKeyId]: publicKey },
      maxFileBytes: Number.MAX_SAFE_INTEGER,
    });
    expect(raisedLimit.ok).toBe(true);
  });

  it("rejects malformed envelope fields and invalid public keys", async () => {
    const pkg = await signedPackage() as Record<string, any>;
    expect(await verifySignedExtensionPackage({ ...pkg, extra: true }, {
      trustedEd25519Keys: { [signerKeyId]: publicKey },
    })).toEqual({ ok: false, error: "package_shape_invalid" });
    expect(await verifySignedExtensionPackage(pkg, {
      trustedEd25519Keys: { [signerKeyId]: "AAAA" },
    })).toEqual({ ok: false, error: "signature_encoding_invalid" });
  });
});
