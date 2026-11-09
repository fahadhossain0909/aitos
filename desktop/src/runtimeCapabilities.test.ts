import { describe, it, expect } from "vitest";
import { capabilityPaths, parseRuntimeCapabilities } from "./runtimeCapabilities";
const now = Date.parse("2026-11-06T12:00:00Z");
const valid = () => ({schema_version: 1, api_version: "1.0", generated_at: new Date(now).toISOString(), controls: "read_only", live_authorized: false, production_certified: false, capabilities: Object.entries(capabilityPaths).map(([id,path]) => ({id,path,available: false})), modules: ["lifecycle"], limitations: []});
describe("runtime negotiation", () => {
  it("accepts fixed read-only capabilities", () => expect(parseRuntimeCapabilities(valid(), now).api_version).toBe("1.0"));
  for (const [key,value] of [["api_version","2.0"],["live_authorized",true],["controls","write"],["production_certified",true],["generated_at","2020-01-01T00:00:00Z"]] as const)
    it(`rejects incompatible ${key}`, () => expect(() => parseRuntimeCapabilities({...valid(),[key]:value},now)).toThrow());
  it("rejects arbitrary endpoint substitution", () => {
    const s=valid(); s.capabilities[0].path="https://other.example/private";
    expect(() => parseRuntimeCapabilities(s,now)).toThrow();
  });
  it("rejects missing or duplicate capabilities", () => {
    const s=valid(); s.capabilities[1]=s.capabilities[0];
    expect(() => parseRuntimeCapabilities(s,now)).toThrow();
    expect(() => parseRuntimeCapabilities({...valid(),capabilities:[]},now)).toThrow();
  });
  it("never accepts runtime control as a read-only capability", () => {
    const s=valid(); s.capabilities.find(c=>c.id==="runtime.control")!.available=true;
    expect(() => parseRuntimeCapabilities(s,now)).toThrow();
  });
});
