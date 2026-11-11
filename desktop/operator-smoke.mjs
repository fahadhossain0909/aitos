import { launchTestBrowser } from "./browserRuntime.mjs";
const url = process.env.AITOS_BROWSER_GATEWAY_URL,
  password = process.env.AITOS_BROWSER_GATEWAY_PASSWORD;
if (!url || !password)
  throw Error("Run through the isolated Python browser harness");
const browser = await launchTestBrowser();
const context = await browser.newContext({
    viewport: { width: 1280, height: 950 },
  }),
  page = await context.newPage(),
  errors = [];
page.on("pageerror", (e) => errors.push(e.message));
page.on("response", (r) => {
  if (r.url().includes("/operator/"))
    console.log(`AUTH_HTTP ${r.status()} ${new URL(r.url()).pathname}`);
});
await page.goto(url);
await page.getByLabel("Operator password").waitFor();
if ((await page.request.get(url + "/console/v1/snapshot")).status() !== 401)
  throw Error("Unauthenticated read allowed");
await page.getByLabel("Operator password").fill("wrong-test-only-password");
await page.getByRole("button", { name: "Open read-only workspace" }).click();
await page.getByRole("alert").filter({ hasText: "rejected" }).waitFor();
await page.getByLabel("Operator password").fill(password);
await page.getByRole("button", { name: "Open read-only workspace" }).click();
await page.getByRole("heading", { name: "Capital, clearly." }).waitFor();
const cookies = await context.cookies();
const session = cookies.find((c) => c.name === "aitos_local_operator");
if (!session?.httpOnly || session.sameSite !== "Strict")
  throw Error("Missing protected cookie flags");
const scriptState = await page.evaluate(() => ({
  cookies: document.cookie,
  storage: JSON.stringify({ ...localStorage, ...sessionStorage }),
}));
if (
  scriptState.cookies.includes(session.value) ||
  scriptState.storage.includes(session.value) ||
  scriptState.storage.includes(password)
)
  throw Error("Credentials exposed to browser storage");
if ((await page.request.get(url + "/console/v1/snapshot")).status() !== 200)
  throw Error("Authenticated read failed");
await page
  .locator("nav")
  .getByRole("button", { name: "Persistence & recovery" })
  .click();
await page.getByText("Last bounded batch verified", { exact: true }).waitFor();
const projected = await page
  .locator(".persistence-metric strong")
  .allTextContents();
if (projected.join(",") !== "0,2,2")
  throw Error("Real isolated projector counters did not reach built UI");
await page.evaluate(() => {
  const tag = document.createElement("div");
  tag.textContent =
    "ISOLATED PAPER PROJECTOR TEST — SYNTHETIC DATA, NOT PRODUCTION EVIDENCE";
  tag.style.cssText =
    "padding:14px;background:#e6d3a0;color:#111;font:700 12px monospace;text-align:center";
  document.querySelector("main").prepend(tag);
  window.scrollTo(0, 0);
});
await page.screenshot({
  path: "../tmp/console/projector-built-end-to-end.png",
  fullPage: true,
});
await page.getByRole("button", { name: "Sample workspace", exact: true }).click();
await page.locator("nav").getByRole("button", { name: "Connections" }).click();
const accountPanel = page.getByRole("region", { name: "Read-only account observations" });
await accountPanel.getByText("100.00000001", { exact: true }).waitFor();
await accountPanel.getByRole("cell", { name: "9900", exact: true }).waitFor();
const protectionPanel = page.getByRole("region", { name: "Protection evidence findings" });
await protectionPanel.getByRole("cell", { name: "not_in_open_inventory", exact: true }).waitFor();
if ((await protectionPanel.textContent()).includes("test-sensitive-protection-id")) throw Error("Raw protection client ID exposed");
const timelinePanel = page.getByRole("region", { name: "Durable account timeline" });
await timelinePanel.getByRole("cell", { name: "99.00000001", exact: true }).first().waitFor();
if ((await page.request.get(url + "/console/v1/account/timeline?cursor=bad")).status() !== 400)
  throw Error("Tampered account timeline cursor accepted");
await page.locator("nav").getByRole("button", { name: "Trade journal" }).click();
await page.getByText(/105 captured/).waitFor();
await page.getByText(/100 loaded records/).waitFor();
await page.getByRole("button", { name: "Next batch" }).click();
await page.getByText(/5 loaded records/).waitFor();
await page.getByRole("button", { name: "Previous batch" }).click();
await page.getByText(/100 loaded records/).waitFor();
console.log("PASS: pinned materialized history forward/back pages reached built UI");
console.log("PASS: synthetic account GET adapter -> durable timeline -> authenticated gateway -> built observation/timeline UI; decimal strings preserved");
await page.getByRole("button", { name: "Sign out", exact: true }).click();
await page.getByLabel("Operator password").waitFor();
if ((await page.request.get(url + "/console/v1/snapshot")).status() !== 401)
  throw Error("Logout did not revoke access");
await page.screenshot({
  path: "../tmp/console/operator-login.png",
  fullPage: true,
});
await page.setViewportSize({ width: 390, height: 844 });
if (
  await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)
)
  throw Error("Mobile login overflow");
await page.screenshot({
  path: "../tmp/console/operator-login-mobile.png",
  fullPage: true,
});
await page.setViewportSize({ width: 1280, height: 950 });
await page.getByLabel("Operator password").fill(password);
await page.getByRole("button", { name: "Open read-only workspace" }).click();
await page.getByRole("heading", { name: "Capital, clearly." }).waitFor();
console.log("ADVANCE_TEST_CLOCK");
await page.getByLabel("Operator password").waitFor({ timeout: 16000 });
if (await page.getByRole("heading", { name: "Capital, clearly." }).count())
  throw Error("Expired session retained financial UI");
await page.getByLabel("Operator password").fill(password);
await page.getByRole("button", { name: "Open read-only workspace" }).click();
await page.getByRole("heading", { name: "Capital, clearly." }).waitFor();
await page.route("**/operator/v1/logout", (route) => route.abort());
await page.getByRole("button", { name: "Sign out", exact: true }).click();
await page
  .getByRole("alert")
  .filter({ hasText: "revocation was not confirmed" })
  .waitFor();
if (await page.getByRole("heading", { name: "Capital, clearly." }).count())
  throw Error("Failed logout retained financial UI");
await page.unroute("**/operator/v1/logout");
await page.getByRole("button", { name: "Retry session revocation" }).click();
await page.getByLabel("Operator password").waitFor();
if (errors.length) throw Error(errors.join("\n"));
console.log(
  "PASS: built authenticated UI, real isolated projector-to-API-to-gateway-to-UI evidence, wrong-password rejection, HttpOnly/SameSite cookie, no credential storage, logout, server expiry, failed-revocation warning, retry, CSP and mobile layout",
);
await browser.close();
