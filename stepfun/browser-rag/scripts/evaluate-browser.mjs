import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFile, stat, mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright";

const output = resolve("artifacts", `evaluation-${Date.now()}`);
const root = resolve("public/models/Xenova/bge-small-zh-v1.5");
const dataset = JSON.parse(await readFile("evaluation.json", "utf8"));
const server = spawn("bash", ["scripts/start.sh"], {
  env: { ...process.env, PORT: "18083", SEEKDB_DATA_DIR: resolve(output, "data") },
  stdio: ["ignore", "pipe", "pipe"],
});
server.stdout.on("data", (chunk) => process.stdout.write(chunk));
server.stderr.on("data", (chunk) => process.stderr.write(chunk));
let browser;

const median = (values) => {
  const sorted = [...values].sort((a, b) => a - b);
  return sorted.length % 2 ? sorted[(sorted.length - 1) / 2] : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2;
};
const average = (values) => values.reduce((sum, value) => sum + value, 0) / values.length;
const bytes = async (file) => (await stat(file)).size;

function quality(documents, queryEmbeddings) {
  return dataset.queries.map((query, queryIndex) => {
    const ranked = documents.map((document) => ({
      id: document.id,
      score: document.embedding.reduce((sum, value, index) => sum + value * queryEmbeddings[queryIndex][index], 0),
    })).sort((a, b) => b.score - a.score || a.id.localeCompare(b.id));
    const top = ranked.slice(0, 2).map((item) => item.id);
    const recall = top.filter((id) => query.relevant.includes(id)).length / query.relevant.length;
    const dcg = ranked.slice(0, 5).reduce((sum, item, index) => sum + (query.relevant.includes(item.id) ? 1 / Math.log2(index + 2) : 0), 0);
    const ideal = query.relevant.slice(0, 5).reduce((sum, _, index) => sum + 1 / Math.log2(index + 2), 0);
    return { query: query.text, relevant: query.relevant, top2: top, recallAt2: recall, ndcgAt5: ideal ? dcg / ideal : 0 };
  });
}

async function measure(config) {
  const page = await browser.newPage();
  const requests = [];
  const errors = [];
  page.on("request", (request) => requests.push(request.url()));
  page.on("pageerror", (error) => errors.push(error.message));
  try {
    await page.goto("http://127.0.0.1:18083", { waitUntil: "domcontentloaded" });
    const model = await page.evaluate(async (options) => {
      const module = await import("/embeddings.js");
      await module.loadModel(() => {}, options);
      return module.backendStatus();
    }, config);
    const adapter = await page.evaluate(async () => {
      if (!navigator.gpu) return null;
      const value = await navigator.gpu.requestAdapter();
      if (!value) return null;
      return {
        vendor: value.info?.vendor || null,
        architecture: value.info?.architecture || null,
        device: value.info?.device || null,
        description: value.info?.description || null,
        isFallbackAdapter: value.isFallbackAdapter ?? null,
      };
    });
    const documents = [];
    const documentBatchTimes = [];
    for (let start = 0; start < dataset.documents.length; start += 4) {
      const batch = dataset.documents.slice(start, start + 4);
      const result = await page.evaluate(async (texts) => {
        const module = await import("/embeddings.js");
        return module.embedTexts(texts, { batchSize: 4 });
      }, batch.map((item) => item.text));
      documentBatchTimes.push(result.elapsedMs);
      batch.forEach((item, index) => documents.push({ id: item.id, embedding: result.embeddings[index] }));
    }
    const queryEmbeddings = [];
    const rawQueryTimes = [];
    for (let round = 0; round < 3; round++) {
      for (const query of dataset.queries) {
        const result = await page.evaluate(async (text) => {
          const module = await import("/embeddings.js");
          return module.embedTexts([text], { query: true, batchSize: 1 });
        }, query.text);
        if (round === 0) queryEmbeddings.push(result.embeddings[0]);
        rawQueryTimes.push({ round, query: query.text, elapsedMs: result.elapsedMs });
      }
    }
    const metrics = quality(documents, queryEmbeddings);
    const commonFiles = ["config.json", "tokenizer.json", "tokenizer_config.json", "vocab.txt", "special_tokens_map.json"];
    const weightFile = { fp32: "model.onnx", fp16: "model_fp16.onnx", q8: "model_quantized.onnx" }[config.dtype];
    assert.ok(requests.some((url) => url.endsWith(`/onnx/${weightFile}`)), `missing actual ${weightFile} request`);
    assert.ok(requests.every((url) => url.startsWith("http://127.0.0.1:18083/") || url.startsWith("blob:") || url.startsWith("data:")), "remote runtime request detected");
    const commonBytes = (await Promise.all(commonFiles.map((file) => bytes(resolve(root, file))))).reduce((sum, size) => sum + size, 0);
    const weightBytes = await bytes(resolve(root, "onnx", weightFile));
    return {
      ...config,
      model,
      adapter,
      runtimeFiles: { commonBytes, weightFile, weightBytes, totalBytes: commonBytes + weightBytes },
      firstLoadAndWarmupMs: model.loadMs,
      documentBatchTimes,
      queryTimes: rawQueryTimes,
      warmQueryMedianMs: median(rawQueryTimes.filter((item) => item.round > 0).map((item) => item.elapsedMs)),
      quality: metrics,
      meanRecallAt2: average(metrics.map((item) => item.recallAt2)),
      meanNdcgAt5: average(metrics.map((item) => item.ndcgAt5)),
      requests,
      errors,
    };
  } finally {
    await page.close();
  }
}

try {
  await mkdir(output, { recursive: true });
  for (let attempt = 0; attempt < 120; attempt++) {
    try {
      if ((await fetch("http://127.0.0.1:18083/api/health")).ok) break;
    } catch {}
    if (server.exitCode !== null) throw new Error(`server exited with ${server.exitCode}`);
    if (attempt === 119) throw new Error("server did not become ready");
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  browser = await chromium.launch({
    executablePath: process.env.CHROME_PATH || "/usr/bin/google-chrome",
    headless: true,
    args: ["--no-sandbox", "--disable-dev-shm-usage", "--enable-unsafe-webgpu"],
  });
  const results = [];
  for (const config of [{ device: "wasm", dtype: "fp32" }, { device: "wasm", dtype: "q8" }]) {
    console.log(`MEASURING ${config.device}/${config.dtype}`);
    results.push(await measure(config));
  }
  try {
    results.push(await measure({ device: "webgpu", dtype: "fp32" }));
  } catch (error) {
    results.push({ device: "webgpu", dtype: "fp32", verified: false, error: error.message });
    console.warn("WebGPU measurement unavailable", error.message);
  }
  const [baseline, quantized] = results;
  const report = {
    success: true,
    model: "Xenova/bge-small-zh-v1.5",
    revision: "75c43b069aac4d136ba6bc1122f995fedcfd2781",
    method: "Actual browser inference; manually labelled six-document smoke set, not a general model quality benchmark",
    compressionRatio: baseline.runtimeFiles.totalBytes / quantized.runtimeFiles.totalBytes,
    results,
  };
  await writeFile(resolve(output, "metrics.json"), JSON.stringify(report, null, 2) + "\n");
  console.log(JSON.stringify(report, null, 2));
} finally {
  if (browser) await browser.close();
  if (server.exitCode === null) server.kill("SIGTERM");
}
