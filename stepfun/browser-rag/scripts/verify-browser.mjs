import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdir, writeFile } from "node:fs/promises";
import { once } from "node:events";
import { resolve } from "node:path";
import { chromium } from "playwright";

const forceWasm = process.argv.includes("--wasm");
const label = `${forceWasm ? "wasm" : "auto"}-${Date.now()}`;
const output = resolve("artifacts", label);
const data = resolve(output, "data");
let server;
let browser;

async function startServer() {
  server = spawn("bash", ["scripts/start.sh"], {
    env: {
      ...process.env,
      PORT: "18082",
      SEEKDB_DATA_DIR: data,
      SEEKDB_BINDINGS_CACHE_DIR: process.env.SEEKDB_BINDINGS_CACHE_DIR || resolve("data/bindings"),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  server.stdout.on("data", (chunk) => process.stdout.write(chunk));
  server.stderr.on("data", (chunk) => process.stderr.write(chunk));
  for (let attempt = 0; attempt < 120; attempt++) {
    try {
      const response = await fetch("http://127.0.0.1:18082/api/health");
      if (response.ok) return;
    } catch {}
    if (server.exitCode !== null) throw new Error(`server exited with ${server.exitCode}`);
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error("server did not become ready");
}

async function stopServer() {
  if (!server || server.exitCode !== null) return;
  const exited = once(server, "exit");
  server.kill("SIGTERM");
  await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 10000))]);
  if (server.exitCode === null) server.kill("SIGKILL");
}

async function search(page) {
  await page.click("#search");
  await page.waitForSelector('[data-testid="result"]', { timeout: 60000 });
  const results = await page.locator('[data-testid="result"]').allTextContents();
  assert.ok(results.some((text) => text.includes("fts.md") && text.includes("倒排结构")));
  return results;
}

try {
  await mkdir(output, { recursive: true });
  await startServer();
  browser = await chromium.launch({
    executablePath: process.env.CHROME_PATH || "/usr/bin/google-chrome",
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--enable-unsafe-webgpu", ...(forceWasm ? ["--disable-webgpu"] : [])],
  });
  const page = await browser.newPage();
  if (forceWasm) await page.addInitScript(() => Object.defineProperty(navigator, "gpu", { value: undefined }));
  const consoleMessages = [];
  const requests = [];
  page.on("console", (message) => {
    consoleMessages.push(`${message.type()}: ${message.text()}`);
    if (["error", "warning"].includes(message.type())) console.error(message.type(), message.text());
  });
  page.on("pageerror", (error) => {
    consoleMessages.push(`pageerror: ${error.message}`);
    console.error(error);
  });
  page.on("request", (request) => requests.push(request.url()));
  await page.goto("http://127.0.0.1:18082", { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelector("#database-status").textContent.includes("已连接"), undefined, { timeout: 60000 });
  await page.click("#load-samples");
  await page.click("#import");
  await page.waitForFunction(() => document.querySelector("#message").textContent.includes("已导入 4"), undefined, { timeout: 600000 });
  const backend = await page.locator("#backend").textContent();
  if (forceWasm) assert.match(backend, /WASM · q8/);
  else console.log(`Actual inference backend: ${backend}`);
  const results = await search(page);
  const searchMetrics = await page.locator("#search-metrics").textContent();

  await page.fill("#new-collection", `verify_${Date.now()}`);
  await page.click("#create-collection");
  await page.waitForFunction(() => document.querySelector("#collection").value.startsWith("verify_"), undefined, { timeout: 60000 });
  const selected = await page.locator("#collection").inputValue();
  assert.match(selected, /^verify_/);
  await page.click("#load-samples");
  await page.click("#import");
  await page.waitForFunction(() => document.querySelector("#message").textContent.includes("已导入 4"), undefined, { timeout: 180000 });
  await search(page);
  await page.selectOption("#collection", "research_notes");
  await search(page);

  await stopServer();
  await startServer();
  await page.reload({ waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.querySelector("#database-status").textContent.includes("已连接"), undefined, { timeout: 60000 });
  await search(page);
  await page.screenshot({ path: resolve(output, "application.png"), fullPage: true });
  const removed = await fetch(`http://127.0.0.1:18082/api/collections/${encodeURIComponent(selected)}`, { method: "DELETE" });
  assert.ok(removed.ok, "temporary collection cleanup failed");
  assert.ok(requests.every((url) => !/openai|dashscope|api\.anthropic/.test(url)), "cloud inference endpoint detected");
  const report = {
    success: true,
    requestedMode: forceWasm ? "wasm" : "auto",
    actualBackend: backend,
    searchMetrics,
    results,
    checks: [
      "browser-local embedding",
      "real seekdb hybrid retrieval",
      "collection switching",
      "server restart recovery",
      "temporary collection cleanup",
      ...(forceWasm ? ["WASM fallback"] : []),
    ],
    requests,
    consoleMessages,
  };
  await writeFile(resolve(output, "verification.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify({ success: true, backend, searchMetrics, evidence: resolve(output, "verification.json") }, null, 2));
} finally {
  if (browser) await browser.close();
  await stopServer();
}
