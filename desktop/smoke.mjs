import { launchTestBrowser } from "./browserRuntime.mjs";
import fs from "node:fs";
const browser = await launchTestBrowser();
const errors = [];
const context = await browser.newContext();
context.on("page", (p) => p.on("pageerror", (e) => errors.push(e.message)));
async function openPage(options) {
  const p = await context.newPage();
  await p.setViewportSize(options.viewport);
  return p;
}
const page = await openPage({ viewport: { width: 1440, height: 1100 } });

await page.goto("http://127.0.0.1:5173");
await page.getByRole("heading", { name: "Capital, clearly." }).waitFor();
await page.screenshot({ path: "../tmp/console/overview.png", fullPage: true });
for (const label of [
  "Portfolio",
  "Trade journal",
  "Performance",
  "Research lab",
  "Observability",
  "Connections",
  "Configuration",
  "Quality & readiness",
  "Persistence & recovery",
]) {
  await page.locator("nav").getByRole("button", { name: label }).click();
  await page.getByRole("heading", { name: label + ".", exact: true }).waitFor();
}
await page.locator("nav").getByRole("button", { name: "Research lab" }).click();
await page.getByRole("button", { name: "Build evaluation plan" }).click();
await page.getByRole("heading", { name: /chronological folds/ }).waitFor();
const planDownload = page.waitForEvent("download");
await page.getByRole("button", { name: "Export plan" }).click();
if (
  (await planDownload).suggestedFilename() !==
  "aitos-research-partition-plan.json"
)
  throw Error("Research export missing");
await page.screenshot({
  path: "../tmp/console/research-planner.png",
  fullPage: true,
});
await page.getByLabel("Rolling training window").fill("0");
await page.getByRole("button", { name: "Build evaluation plan" }).click();
await page.getByRole("alert").waitFor();
if (await page.getByRole("button", { name: "Export plan" }).count())
  throw Error("Invalid input retained stale research plan");
await page.getByLabel("Rolling training window").fill("180");
await page
  .locator("nav")
  .getByRole("button", { name: "Performance", exact: false })
  .click();
await page
  .getByRole("heading", { name: "Performance evidence unavailable" })
  .waitFor();
await page
  .locator("nav")
  .getByRole("button", { name: "Quality & readiness" })
  .click();
await page
  .getByRole("heading", { name: "Trust is a trail. Not a score." })
  .waitFor();
await page.getByText("Live not authorized", { exact: true }).waitFor();
await page.locator(".quality-gate").first().locator("summary").click();
await page.getByText("No runtime modules attached.", { exact: true }).waitFor();
const downloadPromise = page.waitForEvent("download");
await page.getByRole("button", { name: "Export evidence" }).click();
const exported = await downloadPromise;
if (exported.suggestedFilename() !== "aitos-readiness-observations.json")
  throw Error("Readiness export missing");
await page.screenshot({ path: "../tmp/console/readiness.png", fullPage: true });
await page.setViewportSize({ width: 390, height: 844 });
await page.screenshot({
  path: "../tmp/console/readiness-mobile.png",
  fullPage: true,
});
if (
  await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
)
  throw Error("Readiness mobile overflow");
await page.setViewportSize({ width: 1440, height: 1100 });
await page.locator("nav").getByRole("button", { name: "Overview" }).click();
await page
  .getByRole("button", { name: "Sample workspace", exact: false })
  .click();
await page.getByText("NO RUNTIME ATTACHED", { exact: true }).waitFor();
if (await page.getByText("$128,450.25", { exact: true }).count())
  throw Error("Sample equity leaked into runtime");
await page.screenshot({ path: "../tmp/console/runtime.png", fullPage: true });
await page.getByRole("button", { name: "Runtime view", exact: false }).click();
await page.getByRole("row", { name: "Inspect BTCUSDT position" }).click();
await page.getByRole("dialog").waitFor();
await page.keyboard.press("Escape");
await page.getByRole("button", { name: "Live", exact: false }).first().click();
await page.getByRole("dialog").waitFor();
await page.keyboard.press("Escape");
await page.setViewportSize({ width: 390, height: 844 });
await page.screenshot({ path: "../tmp/console/mobile.png", fullPage: true });
const overflow = await page.evaluate(
  () => document.documentElement.scrollWidth > innerWidth,
);
if (overflow) throw Error("Mobile horizontal overflow");
// A synthetic HTTP fixture exercises failure clearing; not exchange/runtime evidence.
const faultPage = await openPage({
  viewport: { width: 1280, height: 900 },
});
const base = await (
  await page.request.get("http://127.0.0.1:5173/console/v1/snapshot")
).json();
let fail = false;
await faultPage.route("**/console/v1/snapshot", (route) =>
  fail
    ? route.abort()
    : route.fulfill({
        json: {
          ...base,
          connected: true,
          source: "runtime",
          generated_at: new Date().toISOString(),
          positions: [
            {
              trade_id: "network-test-fixture",
              symbol: "BTCUSDT",
              side: "LONG",
              state: "position_opened",
              quantity: 1,
              entry_price: 100,
              position_size_usd: 100,
              risk_amount_usd: 1,
              pnl: null,
              entry_time: new Date().toISOString(),
            },
          ],
        },
      }),
);
await faultPage.goto("http://127.0.0.1:5173");
await faultPage
  .getByRole("button", { name: "Sample workspace", exact: false })
  .click();
await faultPage.getByText("RUNTIME ATTACHED", { exact: true }).waitFor();
await faultPage
  .getByRole("row", { name: "Inspect BTCUSDT position" })
  .waitFor();
fail = true;
await faultPage
  .getByText("NO RUNTIME ATTACHED", { exact: true })
  .waitFor({ timeout: 16000 });
if (
  await faultPage.getByRole("row", { name: "Inspect BTCUSDT position" }).count()
)
  throw Error("Failed request retained stale financial data");
await faultPage.close();
// Synthetic cross-language contract fixture, visibly watermarked in screenshots.
const performancePage = await openPage({
  viewport: { width: 1440, height: 1100 },
});
const fixture = JSON.parse(
  fs.readFileSync("fixtures/performance-browser.json", "utf8"),
);
await performancePage.route("**/console/v1/performance", (route) =>
  route.fulfill({
    json: { ...fixture, generated_at: new Date().toISOString() },
  }),
);
await performancePage.goto("http://127.0.0.1:5173");
await performancePage
  .locator("nav")
  .getByRole("button", { name: "Performance", exact: false })
  .click();
await performancePage
  .getByText("CUMULATIVE RECORDED NET P&L", { exact: true })
  .waitFor();
await performancePage.getByLabel("Inspect closed-PnL observation").fill("2");
await performancePage.evaluate(() => {
  const el = document.createElement("div");
  el.textContent = "BROWSER CONTRACT FIXTURE · NOT LIVE ACCOUNT DATA";
  el.style.cssText =
    "padding:14px;text-align:center;background:#e6d3a0;color:#111;font:700 13px monospace;margin-bottom:20px;border-radius:4px";
  document.querySelector("main").prepend(el);
  window.scrollTo(0, 0);
});
await performancePage.screenshot({
  path: "../tmp/console/performance-test-fixture.png",
  fullPage: true,
});
await performancePage.setViewportSize({ width: 390, height: 844 });
if (
  await performancePage.evaluate(
    () => document.documentElement.scrollWidth > innerWidth,
  )
)
  throw Error("Performance mobile overflow");
await performancePage.screenshot({
  path: "../tmp/console/performance-mobile-test-fixture.png",
  fullPage: true,
});
await performancePage.close();
// Persistence: actual unavailable state, explicit synthetic tutorial, export labels.
await page.setViewportSize({ width: 1440, height: 1100 });
await page
  .locator("nav")
  .getByRole("button", { name: "Persistence & recovery" })
  .click();
await page
  .getByRole("heading", { name: "Persistence evidence unavailable" })
  .waitFor();
if (
  !(await page.getByRole("button", { name: "Export observation" }).isDisabled())
)
  throw Error("Unavailable persistence exported as evidence");
await page.getByRole("button", { name: "Explore synthetic example" }).click();
await page
  .getByText("ILLUSTRATIVE WORKFLOW — NOT RUNTIME EVIDENCE", { exact: true })
  .waitFor();
const persistenceDownload = page.waitForEvent("download");
await page.getByRole("button", { name: "Export observation" }).click();
const persistenceFile = await persistenceDownload;
const persistenceJSON = JSON.parse(
  fs.readFileSync(await persistenceFile.path(), "utf8"),
);
if (
  !persistenceJSON.synthetic ||
  persistenceJSON.live_authorized !== false ||
  persistenceJSON.observation.source !== "illustrative"
)
  throw Error("Synthetic export lost provenance");
await page.screenshot({
  path: "../tmp/console/persistence-illustration.png",
  fullPage: true,
});
await page.setViewportSize({ width: 390, height: 844 });
if (
  await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
)
  throw Error("Persistence mobile overflow");
await page.screenshot({
  path: "../tmp/console/persistence-mobile.png",
  fullPage: true,
});
await page.getByRole("button", { name: "Return to observed evidence" }).click();
await page
  .getByRole("heading", { name: "Persistence evidence unavailable" })
  .waitFor();
const persistencePage = await openPage({
  viewport: { width: 1440, height: 1100 },
});
const projectionFixture = JSON.parse(
  fs.readFileSync("src/projection.fixture.json", "utf8"),
);
let malformedProjection = false;
await persistencePage.route("**/console/v1/snapshot", (route) =>
  route.fulfill({
    json: {
      ...base,
      generated_at: new Date().toISOString(),
      projection: {
        ...projectionFixture,
        observed_at: new Date().toISOString(),
        counts: malformedProjection
          ? { total: 12, pending: 3, delivered: 99 }
          : projectionFixture.counts,
      },
    },
  }),
);
await persistencePage.goto("http://127.0.0.1:5173");
await persistencePage
  .locator("nav")
  .getByRole("button", { name: "Persistence & recovery" })
  .click();
await persistencePage
  .getByText("Last bounded batch verified", { exact: true })
  .waitFor();
await persistencePage.evaluate(() => {
  const el = document.createElement("div");
  el.textContent =
    "SYNTHETIC BROWSER CONTRACT FIXTURE — NOT ACCOUNT OR PRODUCTION EVIDENCE";
  el.style.cssText =
    "padding:14px;background:#e6d3a0;color:#111;text-align:center;font:700 12px monospace";
  document.querySelector("main").prepend(el);
  window.scrollTo(0, 0);
});
await persistencePage.screenshot({
  path: "../tmp/console/persistence-contract-fixture.png",
  fullPage: true,
});
const actualDownload = persistencePage.waitForEvent("download");
await persistencePage
  .getByRole("button", { name: "Export observation" })
  .click();
const actualJSON = JSON.parse(
  fs.readFileSync(await (await actualDownload).path(), "utf8"),
);
if (
  actualJSON.synthetic !== false ||
  actualJSON.observation.counts.delivered !== 9
)
  throw Error("Observed export invalid");
malformedProjection = true;
await persistencePage
  .getByRole("heading", { name: "Persistence evidence unavailable" })
  .waitFor({ timeout: 16000 });
if (
  !(await persistencePage
    .getByRole("button", { name: "Export observation" })
    .isDisabled())
)
  throw Error("Malformed projection retained export");
if (
  (
    await persistencePage
      .locator(".persistence-metric strong")
      .allTextContents()
  ).some((value) => value.trim() !== "—")
)
  throw Error("Malformed projection retained counters");
await persistencePage.close();
if (errors.length) throw Error(errors.join("\n"));
console.log(
  "PASS: ten pages, persistence evidence/provenance/export/malformed clearing/mobile, research planner/export/invalid-input clearing, real unavailable performance and synthetic performance contract, readiness evidence/export, runtime/sample isolation, position/live dialogs, mobile overflow, stale-financial clearing on network failure, no JS errors",
);
await browser.close();
