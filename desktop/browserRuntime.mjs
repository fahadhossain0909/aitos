// Linux-only test harness. No trading/runtime launch and no production browser flags.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";
import { brotliDecompressSync } from "node:zlib";
import { chromium as playwright } from "@playwright/test";
import chromium from "@sparticuz/chromium";
export async function launchTestBrowser() {
  const directory = path.dirname(fileURLToPath(import.meta.url));
  const libs = path.resolve(directory, "../tmp/console/chromium-test-runtime");
  fs.mkdirSync(libs, { recursive: true });
  if (!fs.existsSync(path.join(libs, "lib/libnspr4.so"))) {
    const archive = path.join(libs, "al2023.tar");
    const packed = path.join(
      directory,
      "node_modules/@sparticuz/chromium/bin/al2023.tar.br",
    );
    fs.writeFileSync(archive, brotliDecompressSync(fs.readFileSync(packed)));
    execFileSync("tar", ["xf", archive, "-C", libs], { stdio: "pipe" });
  }
  const env = {
    ...process.env,
    LD_LIBRARY_PATH: [path.join(libs, "lib"), process.env.LD_LIBRARY_PATH]
      .filter(Boolean)
      .join(":"),
  };
  for (const key of [
    "AITOS_OPERATOR_PASSWORD",
    "AITOS_CONSOLE_TOKEN",
    "AITOS_BROWSER_GATEWAY_PASSWORD",
  ])
    delete env[key];
  return playwright.launch({
    executablePath: await chromium.executablePath(),
    env,
    args: chromium.args.filter(
      (arg) =>
        ![
          "--disable-web-security",
          "--allow-running-insecure-content",
          "--disable-site-isolation-trials",
        ].includes(arg) && !arg.startsWith("--disable-features="),
    ),
    headless: true,
  });
}
