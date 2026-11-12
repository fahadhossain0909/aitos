export const EXTENSION_CAPABILITIES = [
  "market-data.read",
  "portfolio.read",
  "research.run",
  "workspace.panel",
] as const;

export type ExtensionCapability = (typeof EXTENSION_CAPABILITIES)[number];

export interface ExtensionManifest {
  apiVersion: 1;
  id: string;
  name: string;
  version: string;
  description: string;
  entrypoint: string;
  capabilities: ExtensionCapability[];
}

export interface RegisteredExtension {
  manifest: ExtensionManifest;
  enabled: boolean;
  approved: boolean;
}

export type ManifestValidation =
  | { ok: true; manifest: ExtensionManifest }
  | { ok: false; errors: string[] };

const idPattern = /^[a-z0-9]+(?:[.-][a-z0-9]+){1,5}$/;
const versionPattern = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$/;
const entryPattern = /^[A-Za-z0-9._/-]+\.(?:js|mjs)$/;
const capabilities = new Set<string>(EXTENSION_CAPABILITIES);

export function validateExtensionManifest(input: unknown): ManifestValidation {
  const errors: string[] = [];
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    return { ok: false, errors: ["manifest_must_be_an_object"] };
  }
  const value = input as Record<string, unknown>;
  if (value.apiVersion !== 1) errors.push("unsupported_api_version");
  if (typeof value.id !== "string" || !idPattern.test(value.id)) errors.push("invalid_extension_id");
  if (typeof value.name !== "string" || value.name.trim().length < 2 || value.name.length > 80) errors.push("invalid_extension_name");
  if (typeof value.version !== "string" || !versionPattern.test(value.version)) errors.push("invalid_semver");
  if (typeof value.description !== "string" || value.description.trim().length > 280) errors.push("invalid_description");
  if (
    typeof value.entrypoint !== "string" ||
    !entryPattern.test(value.entrypoint) ||
    value.entrypoint.startsWith("/") ||
    value.entrypoint.includes("\\") ||
    value.entrypoint.split("/").some((part) => part === ".." || part === "." || part === "")
  ) errors.push("unsafe_entrypoint");
  if (!Array.isArray(value.capabilities) || value.capabilities.length > EXTENSION_CAPABILITIES.length) {
    errors.push("invalid_capabilities");
  } else {
    const seen = new Set<string>();
    for (const capability of value.capabilities) {
      if (typeof capability !== "string" || !capabilities.has(capability) || seen.has(capability)) {
        errors.push("invalid_capabilities");
        break;
      }
      seen.add(capability);
    }
  }
  if (errors.length) return { ok: false, errors: [...new Set(errors)] };
  return {
    ok: true,
    manifest: {
      apiVersion: 1,
      id: value.id as string,
      name: (value.name as string).trim(),
      version: value.version as string,
      description: (value.description as string).trim(),
      entrypoint: value.entrypoint as string,
      capabilities: [...(value.capabilities as ExtensionCapability[])],
    },
  };
}

export function registerExtension(
  registry: ReadonlyMap<string, RegisteredExtension>,
  input: unknown,
): { registry: Map<string, RegisteredExtension>; result: ManifestValidation } {
  const result = validateExtensionManifest(input);
  if (!result.ok) return { registry: new Map(registry), result };
  if (registry.has(result.manifest.id)) {
    return {
      registry: new Map(registry),
      result: { ok: false, errors: ["extension_id_already_registered"] },
    };
  }
  const next = new Map(registry);
  next.set(result.manifest.id, {
    manifest: result.manifest,
    enabled: false,
    approved: false,
  });
  return { registry: next, result };
}

export function approveExtension(
  registry: ReadonlyMap<string, RegisteredExtension>,
  id: string,
  approved: boolean,
): Map<string, RegisteredExtension> {
  const next = new Map(registry);
  const current = next.get(id);
  if (current) next.set(id, { ...current, approved, enabled: approved ? current.enabled : false });
  return next;
}

export function setExtensionEnabled(
  registry: ReadonlyMap<string, RegisteredExtension>,
  id: string,
  enabled: boolean,
): Map<string, RegisteredExtension> {
  const next = new Map(registry);
  const current = next.get(id);
  if (current) next.set(id, { ...current, enabled: enabled && current.approved });
  return next;
}

export function hasExtensionCapability(
  registry: ReadonlyMap<string, RegisteredExtension>,
  id: string,
  capability: ExtensionCapability,
): boolean {
  const extension = registry.get(id);
  return Boolean(
    extension?.approved &&
    extension.enabled &&
    extension.manifest.capabilities.includes(capability),
  );
}
