import { describe, expect, it } from "vitest";
import { getDevServerPolicy } from "./devServerPolicy";

describe("Vite development server security policy", () => {
  it("binds to loopback by default", () => {
    expect(getDevServerPolicy({})).toEqual({ host: "127.0.0.1" });
  });

  it("requires an explicit hostname allowlist for remote preview binding", () => {
    expect(() => getDevServerPolicy({ AITOS_VITE_HOST: "0.0.0.0" })).toThrow(
      "requires AITOS_VITE_ALLOWED_HOSTS",
    );
    expect(
      getDevServerPolicy({
        AITOS_VITE_HOST: "0.0.0.0",
        AITOS_VITE_ALLOWED_HOSTS: "preview.example.test",
      }),
    ).toEqual({
      host: "0.0.0.0",
      allowedHosts: ["preview.example.test"],
    });
  });

  it("rejects wildcard and URL-like host allowlist entries", () => {
    for (const value of [
      "*",
      "https://preview.example.test",
      "preview.example.test/path",
      "preview.example.test:5173",
      ".example.test",
      "*.example.test",
      "preview..example.test",
    ]) {
      expect(() =>
        getDevServerPolicy({
          AITOS_VITE_HOST: "0.0.0.0",
          AITOS_VITE_ALLOWED_HOSTS: value,
        }),
      ).toThrow("exact DNS hostnames");
    }
  });
});
