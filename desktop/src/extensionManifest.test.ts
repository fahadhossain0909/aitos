import { describe, expect, it } from "vitest";
import {
  approveExtension,
  hasExtensionCapability,
  registerExtension,
  setExtensionEnabled,
  validateExtensionManifest,
} from "./extensionManifest";

const valid = {
  apiVersion: 1,
  id: "aitos.example.indicator",
  name: "Example Indicator",
  version: "1.2.0",
  description: "A bounded example indicator extension.",
  entrypoint: "dist/index.mjs",
  capabilities: ["market-data.read", "workspace.panel"],
};

describe("extension manifest security contract", () => {
  it("accepts a valid, bounded manifest", () => {
    const result = validateExtensionManifest(valid);
    expect(result.ok).toBe(true);
  });

  it("rejects unknown permissions and duplicate capabilities", () => {
    expect(validateExtensionManifest({ ...valid, capabilities: ["shell.exec"] }).ok).toBe(false);
    expect(validateExtensionManifest({ ...valid, capabilities: ["portfolio.read", "portfolio.read"] }).ok).toBe(false);
  });

  it("rejects path traversal, absolute paths and remote entrypoints", () => {
    const backslashPath = "dist" + String.fromCharCode(92) + "plugin.js";
    for (const entrypoint of ["../outside.js", "/tmp/plugin.js", "https://example.com/a.js", backslashPath]) {
      expect(validateExtensionManifest({ ...valid, entrypoint }).ok).toBe(false);
    }
  });

  it("registers extensions disabled and unapproved by default", () => {
    const result = registerExtension(new Map(), valid);
    expect(result.result.ok).toBe(true);
    const extension = result.registry.get(valid.id);
    expect(extension?.enabled).toBe(false);
    expect(extension?.approved).toBe(false);
  });

  it("requires explicit approval before enabling and capability use", () => {
    const registered = registerExtension(new Map(), valid).registry;
    const premature = setExtensionEnabled(registered, valid.id, true);
    expect(premature.get(valid.id)?.enabled).toBe(false);
    const approved = approveExtension(premature, valid.id, true);
    const enabled = setExtensionEnabled(approved, valid.id, true);
    expect(hasExtensionCapability(enabled, valid.id, "market-data.read")).toBe(true);
    expect(hasExtensionCapability(enabled, valid.id, "portfolio.read")).toBe(false);
  });

  it("revoking approval disables the extension immediately", () => {
    const registered = registerExtension(new Map(), valid).registry;
    const enabled = setExtensionEnabled(approveExtension(registered, valid.id, true), valid.id, true);
    const revoked = approveExtension(enabled, valid.id, false);
    expect(revoked.get(valid.id)?.enabled).toBe(false);
    expect(hasExtensionCapability(revoked, valid.id, "market-data.read")).toBe(false);
  });

  it("rejects duplicate extension identifiers without replacing the original", () => {
    const first = registerExtension(new Map(), valid).registry;
    const duplicate = registerExtension(first, { ...valid, name: "Changed name" });
    expect(duplicate.result.ok).toBe(false);
    expect(duplicate.registry.get(valid.id)?.manifest.name).toBe("Example Indicator");
  });
});
