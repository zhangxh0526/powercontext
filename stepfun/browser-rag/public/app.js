import { backendStatus, chunkText, embedTexts, loadModel } from "/embeddings.js";

const $ = (id) => document.getElementById(id);
let busy = false;
let samples = [];
let sampleFiles = [];

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: options.body ? { "Content-Type": "application/json" } : undefined,
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function message(text, error = false) {
  $("message").textContent = text;
  $("message").className = error ? "error" : "success";
  if (error) console.error(text);
}

function setBusy(value) {
  busy = value;
  for (const id of ["import", "search", "create-collection", "delete-collection", "load-samples", "collection", "documents", "new-collection", "query", "keyword"]) {
    $(id).disabled = value;
  }
  if (!value) $("import").disabled = $("documents").files.length === 0 && sampleFiles.length === 0;
}

function showBackend() {
  const status = backendStatus();
  if (status) {
    $("backend").textContent = `${status.device === "webgpu" ? "WebGPU" : "WASM"} · ${status.dtype} · 加载/预热 ${status.loadMs.toFixed(1)} ms`;
  }
}

function modelProgress(progress) {
  if (progress.status === "progress" && progress.file) {
    const percent = progress.total ? ` ${(100 * progress.loaded / progress.total).toFixed(0)}%` : "";
    $("model-progress").textContent = `模型文件 ${progress.file}${percent}`;
  }
}

async function refreshCollections(selected) {
  const { collections, current } = await api("/api/collections");
  $("collection").replaceChildren();
  for (const collection of collections) {
    const option = document.createElement("option");
    option.value = collection;
    option.textContent = collection;
    $("collection").append(option);
  }
  $("collection").value = selected || current;
  $("database-status").textContent = "seekdb 已连接";
}

async function createCollection() {
  const name = $("new-collection").value.trim();
  if (!name) return message("请输入知识库名称", true);
  setBusy(true);
  try {
    await api("/api/collections", { method: "POST", body: JSON.stringify({ name }) });
    await refreshCollections(name);
    $("new-collection").value = "";
    message(`已创建并切换到 ${name}`);
  } catch (error) {
    message(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function deleteCollection() {
  const name = $("collection").value;
  if (!name || !window.confirm(`删除知识库 ${name} 及其中的文档？`)) return;
  setBusy(true);
  try {
    await api(`/api/collections/${encodeURIComponent(name)}`, { method: "DELETE" });
    $("results").replaceChildren();
    await refreshCollections();
    message("知识库已删除");
  } catch (error) {
    message(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function importDocuments() {
  const files = [...$("documents").files, ...sampleFiles];
  if (!files.length) return message("请先选择文档或加载演示语料", true);
  setBusy(true);
  $("import-progress").value = 0;
  try {
    await loadModel(modelProgress);
    showBackend();
    const documents = [];
    for (const file of files) {
      if (file.size > 1024 * 1024) throw new Error(`${file.name} 超过 1 MiB`);
      const text = await file.text();
      const chunks = chunkText(text).map((content, index) => ({
        id: `${file.name}:${index}`,
        content,
        metadata: { filename: file.name, chunk: index },
      }));
      if (chunks.length > 64) throw new Error(`${file.name} 分块超过 64 块，请拆分文件后导入`);
      documents.push({ chunks });
    }
    const total = documents.reduce((sum, document) => sum + document.chunks.length, 0);
    if (!total) throw new Error("文档没有可导入的文字");
    let imported = 0;
    for (const document of documents) {
      const embeddings = [];
      for (let start = 0; start < document.chunks.length; start += 8) {
        const batch = document.chunks.slice(start, start + 8);
        const result = await embedTexts(batch.map((chunk) => chunk.content));
        embeddings.push(...result.embeddings);
        $("model-progress").textContent = `最近一批 ${batch.length} 块向量化 ${result.elapsedMs.toFixed(1)} ms`;
      }
      const result = await api("/api/documents", {
        method: "POST",
        body: JSON.stringify({ collection: $("collection").value, chunks: document.chunks, embeddings }),
      });
      imported += result.count;
      $("import-progress").value = imported / total;
      $("model-progress").textContent += `；写入/刷新 ${result.elapsedMs.toFixed(1)} ms`;
    }
    sampleFiles = [];
    $("documents").value = "";
    message(`已导入 ${imported} 个文档块；后续可继续增量导入`);
  } catch (error) {
    message(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function search(event) {
  event.preventDefault();
  setBusy(true);
  $("results").replaceChildren();
  try {
    await loadModel(modelProgress);
    showBackend();
    const started = performance.now();
    const { embeddings, elapsedMs } = await embedTexts([$("query").value], { query: true, batchSize: 1 });
    const result = await api("/api/search", {
      method: "POST",
      body: JSON.stringify({
        collection: $("collection").value,
        query: $("query").value,
        keyword: $("keyword").value,
        embedding: embeddings[0],
      }),
    });
    $("search-metrics").textContent = `查询向量化 ${elapsedMs.toFixed(1)} ms · seekdb 检索 ${result.elapsedMs.toFixed(1)} ms · 端到端 ${(performance.now() - started).toFixed(1)} ms`;
    if (!result.results.length) {
      $("results").textContent = "没有匹配结果，请更换关键词或先导入文档。";
    }
    for (const item of result.results) {
      const article = document.createElement("article");
      article.className = "result";
      article.dataset.testid = "result";
      const title = document.createElement("h3");
      title.textContent = item.metadata?.filename || item.id;
      const content = document.createElement("p");
      content.textContent = item.document;
      const meta = document.createElement("div");
      meta.className = "result-meta";
      meta.textContent = `${item.id} · ${JSON.stringify(item.score ?? item.distance ?? "seekdb hybrid")}`;
      article.append(title, content, meta);
      $("results").append(article);
    }
  } catch (error) {
    message(error.message, true);
  } finally {
    setBusy(false);
  }
}

$("create-collection").addEventListener("click", createCollection);
$("delete-collection").addEventListener("click", deleteCollection);
$("collection").addEventListener("change", () => $("results").replaceChildren());
$("documents").addEventListener("change", () => { sampleFiles = []; setBusy(false); });
$("import").addEventListener("click", importDocuments);
$("search-form").addEventListener("submit", search);
$("load-samples").addEventListener("click", () => {
  sampleFiles = samples.map((item) => new File([item.content], item.filename, { type: "text/markdown" }));
  $("documents").value = "";
  setBusy(false);
  message(`已选择 ${sampleFiles.length} 篇演示文档，点击“本地向量化并导入”继续`);
});

Promise.all([api("/api/health"), api("/api/samples")]).then(([, body]) => {
  samples = body.samples;
  return refreshCollections();
}).catch((error) => {
  $("database-status").textContent = "连接失败";
  message(error.message, true);
});
