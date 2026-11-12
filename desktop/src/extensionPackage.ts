import { validateExtensionManifest, type ExtensionManifest } from "./extensionManifest";

export interface SignedExtensionPackage {
  manifest: unknown;
  files: Record<string, string>;
  signerKeyId: string;
  signature: string;
}

export type ExtensionVerification =
  | { ok: true; manifest: ExtensionManifest; files: Map<string, Uint8Array>; signerKeyId: string }
  | { ok: false; error: string };

export interface ExtensionTrustPolicy {
  trustedEd25519Keys: Readonly<Record<string, string>>;
  maxPackageBytes?: number;
  maxFileBytes?: number;
}

const MAX_PACKAGE_BYTES = 5 * 1024 * 1024;
const MAX_FILE_BYTES = 2 * 1024 * 1024;
const MAX_FILES = 128;
const encoder = new TextEncoder();

function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const record = value as Record<string, unknown>;
  return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${canonical(record[key])}`).join(",")}}`;
}

function decodeBase64(value: string): Uint8Array | null {
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) return null;
  try {
    const binary = atob(value);
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}

function safePackagePath(path: string): boolean {
  return path.length > 0 &&
    path.length <= 240 &&
    !path.startsWith("/") &&
    !path.includes("\\\\") &&
    !path.includes("\\0") &&
    path.split("/").every((part) => part !== "" && part !== "." && part !== "..") &&
    /^(?:[A-Za-z0-9._-]+\\/)*[A-Za-z0-9._-]+\\.(?:js|mjs|css|json|svg|png|woff2?)$/.test(path);
}

async function sha256(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export async function verifySignedExtensionPackage(
  input: unknown,
  policy: ExtensionTrustPolicy,
): Promise<ExtensionVerification> {
  if (!input || typeof input !== "object" || Array.isArray(input)) return { ok: false, error: "package_shape_invalid" };
  const value = input as Record<string, unknown>;
  if (Object.keys(value).sort().join(",") !== "files,manifest,signature,signerKeyId") {
    return { ok: false, error: "package_shape_invalid" };
  }
  const validated = validateExtensionManifest(value.manifest);
  if (!validated.ok) return { ok: false, error: "manifest_invalid" };
  if (typeof value.signerKeyId !== "string" || !/^[a-z0-9][a-z0-9._-]{2,63}$/.test(value.signerKeyId)) {
    return { ok: false, error: "signer_key_id_invalid" };
  }
  if (typeof value.signature !== "string") return { ok: false, error: "signature_invalid" };
  const publicKeyText = policy.trustedEd25519Keys[value.signerKeyId];
  if (!publicKeyText) return { ok: false, error: "publisher_not_trusted" };
  if (!value.files || typeof value.files !== "object" || Array.isArray(value.files)) {
    return { ok: false, error: "package_files_invalid" };
  }
  const rawFiles = value.files as Record<string, unknown>;
  const paths = Object.keys(rawFiles).sort();
  if (!paths.length || paths.length > MAX_FILES || !paths.includes(validated.manifest.entrypoint)) {
    return { ok: false, error: "package_file_count_or_entrypoint_invalid" };
  }
  const maxPackageBytes = policy.maxPackageBytes ?? MAX_PACKAGE_BYTES;
  const maxFileBytes = policy.maxFileBytes ?? MAX_FILE_BYTES;
  const files = new Map<string, Uint8Array>();
  let packageBytes = 0;
  const digests: Record<string, string> = {};
  for (const path of paths) {
    const encoded = rawFiles[path];
    if (!safePackagePath(path) || typeof encoded !== "string") return { ok: false, error: "package_path_or_encoding_invalid" };
    const bytes = decodeBase64(encoded);
    if (!bytes || bytes.byteLength > maxFileBytes) return { ok: false, error: "package_file_size_invalid" };
    packageBytes += bytes.byteLength;
    if (packageBytes > maxPackageBytes) return { ok: false, error: "package_size_invalid" };
    files.set(path, bytes);
    digests[path] = await sha256(bytes);
  }
  const publicKey = decodeBase64(publicKeyText);
  const signature = decodeBase64(value.signature);
  if (!publicKey || publicKey.byteLength !== 32 || !signature || signature.byteLength !== 64) {
    return { ok: false, error: "signature_encoding_invalid" };
  }
  const signedPayload = encoder.encode(canonical({
    apiVersion: 1,
    manifest: validated.manifest,
    files: digests,
    signerKeyId: value.signerKeyId,
  }));
  try {
    const key = await crypto.subtle.importKey("raw", publicKey, { name: "Ed25519" }, false, ["verify"]);
    const verified = await crypto.subtle.verify({ name: "Ed25519" }, key, signature, signedPayload);
    if (!verified) return { ok: false, error: "signature_verification_failed" };
  } catch {
    return { ok: false, error: "signature_verification_unavailable" };
  }
  return { ok: true, manifest: validated.manifest, files, signerKeyId: value.signerKeyId };
}

/**
 * Canonical bytes signed by an extension publisher. Exported so publisher tooling
 * and verification tests use exactly the same deterministic payload contract.
 */
export async function extensionSignaturePayload(
  manifestInput: unknown,
  filesInput: Readonly<Record<string, string>>,
  signerKeyId: string,
): Promise<Uint8Array> {
  const validated = validateExtensionManifest(manifestInput);
  if (!validated.ok) throw new Error("manifest_invalid");
  const paths = Object.keys(filesInput).sort();
  const digests: Record<string, string> = {};
  for (const path of paths) {
    if (!safePackagePath(path)) throw new Error("package_path_invalid");
    const bytes = decodeBase64(filesInput[path]);
    if (!bytes) throw new Error("package_file_encoding_invalid");
    digests[path] = await sha256(bytes);
  }
  return encoder.encode(canonical({ apiVersion: 1, manifest: validated.manifest, files: digests, signerKeyId }));
}
