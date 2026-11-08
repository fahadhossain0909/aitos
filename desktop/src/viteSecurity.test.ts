import { it, expect, vi } from "vitest";
import config from "../vite.config";
const environment = {
  command: "serve" as const,
  mode: "development",
  isSsrBuild: false,
  isPreview: false,
};
it("refuses a server-side runtime bearer in the development proxy", () => {
  vi.stubEnv("AITOS_CONSOLE_TOKEN", "test-only-not-a-real-runtime-credential");
  try {
    expect(() => typeof config === "function" && config(environment)).toThrow(
      /authenticated operator gateway/,
    );
  } finally {
    vi.unstubAllEnvs();
  }
});
it("development config never injects runtime authorization headers", async () => {
  vi.stubEnv("AITOS_CONSOLE_TOKEN", "");
  try {
    const result =
      typeof config === "function" ? await config(environment) : await config;
    const proxy = result.server?.proxy?.["/console"];
    expect(typeof proxy).toBe("object");
    expect(proxy).not.toHaveProperty("headers");
    expect(proxy).toHaveProperty("configure");
  } finally {
    vi.unstubAllEnvs();
  }
});
it("strips browser credentials from the development adapter request", async () => {
  vi.stubEnv("AITOS_CONSOLE_TOKEN", "");
  try {
    const result =
      typeof config === "function" ? await config(environment) : await config;
    const proxy = result.server?.proxy?.["/console"];
    if (typeof proxy !== "object" || !proxy.configure)
      throw Error("Missing safe proxy configuration");
    const removeHeader = vi.fn();
    const on = vi.fn(
      (
        _event: string,
        callback: (request: { removeHeader: typeof removeHeader }) => void,
      ) => {
        callback({ removeHeader });
      },
    );
    proxy.configure({ on } as never, proxy);
    expect(removeHeader.mock.calls.map((call) => call[0])).toEqual([
      "authorization",
      "proxy-authorization",
      "cookie",
    ]);
  } finally {
    vi.unstubAllEnvs();
  }
});
