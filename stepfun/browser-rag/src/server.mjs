import { createReadStream } from "node:fs";
import { readFile, stat } from "node:fs/promises";
import { createServer } from "node:http";
import { dirname, extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { InputError, KnowledgeBase } from "./database.mjs";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PUBLIC = resolve(ROOT, "public");
const MODULES = resolve(ROOT, "node_modules");
const PORT = Number(process.env.PORT || 8080);
const database = new KnowledgeBase(process.env.SEEKDB_DATA_DIR || resolve(ROOT, "data"));
const samples = JSON.parse(await readFile(resolve(ROOT, "samples.json"), "utf8"));
const MIME = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".onnx": "application/octet-stream",
  ".wasm": "application/wasm",
};

function json(response, status, body) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  response.end(JSON.stringify(body));
}

async function body(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 8 * 1024 * 1024) throw new InputError("请求体不能超过 8 MiB");
    chunks.push(chunk);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8"));
  } catch {
    throw new InputError("请求体必须是 JSON 对象");
  }
}

async function api(request, response, pathname) {
  if (pathname === "/api/health" && request.method === "GET") {
    await database.list();
    return json(response, 200, { status: "ok", database: "seekdb", embedding: "browser" });
  }
  if (pathname === "/api/samples" && request.method === "GET") return json(response, 200, { samples });
  if (pathname === "/api/collections" && request.method === "GET") return json(response, 200, await database.list());
  if (pathname === "/api/collections" && request.method === "POST") {
    const input = await body(request);
    return json(response, 201, await database.create(input.name));
  }
  if (pathname.startsWith("/api/collections/") && request.method === "DELETE") {
    return json(response, 200, await database.remove(decodeURIComponent(pathname.slice("/api/collections/".length))));
  }
  if (pathname === "/api/documents" && request.method === "POST") {
    const input = await body(request);
    return json(response, 200, await database.upsert(input.collection, input.chunks, input.embeddings));
  }
  if (pathname === "/api/search" && request.method === "POST") {
    const input = await body(request);
    return json(response, 200, await database.search(input.collection, input.query, input.keyword, input.embedding));
  }
  return json(response, 404, { error: "接口不存在" });
}

async function staticFile(response, pathname) {
  const relative = pathname === "/" ? "index.html" : decodeURIComponent(pathname.slice(1));
  const root = relative.startsWith("node_modules/") ? MODULES : PUBLIC;
  const path = resolve(root, relative.startsWith("node_modules/") ? relative.slice(13) : relative);
  if (!path.startsWith(root + sep)) return json(response, 403, { error: "路径不允许" });
  const info = await stat(path);
  if (!info.isFile()) return json(response, 404, { error: "文件不存在" });
  response.writeHead(200, {
    "Content-Type": MIME[extname(path)] || "application/octet-stream",
    "Content-Length": info.size,
    "Cache-Control": "no-cache",
    "X-Content-Type-Options": "nosniff",
  });
  createReadStream(path).pipe(response);
}

const server = createServer(async (request, response) => {
  try {
    const pathname = new URL(request.url, "http://localhost").pathname;
    if (pathname.startsWith("/api/")) await api(request, response, pathname);
    else if (request.method === "GET") await staticFile(response, pathname);
    else json(response, 405, { error: "请求方法不允许" });
  } catch (error) {
    if (response.headersSent) return response.destroy(error);
    if (!(error instanceof InputError)) console.error(error);
    json(response, error instanceof InputError ? 400 : 500, { error: error.message });
  }
});

server.listen(PORT, "127.0.0.1", () => console.log(`Browser RAG: http://127.0.0.1:${PORT}`));
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, () => server.close(async () => {
    await database.close();
    process.exit(0);
  }));
}
