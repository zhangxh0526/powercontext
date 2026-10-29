import { createHash } from "node:crypto";
import { FulltextIndexConfig, Schema, SeekdbClient, VectorIndexConfig } from "seekdb";

export const DIMENSION = 512;
const DEFAULT_COLLECTION = "research_notes";
const NAME_PATTERN = /^[a-zA-Z0-9_]{1,40}$/;

export class InputError extends Error {}

function checkName(name) {
  if (typeof name !== "string" || !NAME_PATTERN.test(name)) {
    throw new InputError("知识库名称只能包含 1–40 个英文字母、数字或下划线");
  }
  return name;
}

function checkText(value, label, maxLength) {
  if (typeof value !== "string" || !value.trim() || value.length > maxLength) {
    throw new InputError(`${label}必须是 1–${maxLength} 个字符的非空字符串`);
  }
  return value;
}

function checkVector(vector) {
  if (!Array.isArray(vector) || vector.length !== DIMENSION || vector.some((value) => !Number.isFinite(value))) {
    throw new InputError(`向量必须是 ${DIMENSION} 个有限数值`);
  }
  return vector;
}

export class KnowledgeBase {
  constructor() {
    this.client = null;
    this.queue = Promise.resolve();
  }

  async connect() {
    if (!this.client) {
      const port = Number(process.env.SEEKDB_PORT || 12881);
      if (!Number.isInteger(port) || port < 1 || port > 65535) throw new InputError("seekdb 端口无效");
      this.client = new SeekdbClient({
        host: process.env.SEEKDB_HOST || "127.0.0.1",
        port,
        user: process.env.SEEKDB_USER || "root",
        password: process.env.SEEKDB_PASSWORD || "",
        database: process.env.SEEKDB_DATABASE || "test",
      });
    }
    return this.client;
  }

  run(operation) {
    const result = this.queue.then(operation, operation);
    this.queue = result.catch(() => {});
    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("seekdb 操作超时")), 60000);
      timer.unref();
    });
    return Promise.race([result, timeout]).finally(() => clearTimeout(timer));
  }

  async getCollection(name) {
    const client = await this.connect();
    return client.getOrCreateCollection({
      name: checkName(name),
      schema: new Schema()
        .createIndex(new FulltextIndexConfig("ik", { ik_mode: "smart" }))
        .createIndex(new VectorIndexConfig({
          hnsw: { dimension: DIMENSION, distance: "cosine" },
          embeddingFunction: null,
        })),
      embeddingFunction: null,
    });
  }

  async list() {
    return this.run(async () => {
      const client = await this.connect();
      const collections = await client.listCollections({ withEmbeddingFunction: false });
      const names = collections.map((collection) => collection.name);
      if (!names.includes(DEFAULT_COLLECTION)) {
        await this.getCollection(DEFAULT_COLLECTION);
        names.push(DEFAULT_COLLECTION);
      }
      return { collections: names.sort(), current: names.includes(DEFAULT_COLLECTION) ? DEFAULT_COLLECTION : names[0] };
    });
  }

  async create(name) {
    return this.run(async () => {
      const client = await this.connect();
      checkName(name);
      if (await client.hasCollection(name)) throw new InputError("知识库已存在");
      await this.getCollection(name);
      return { name };
    });
  }

  async remove(name) {
    return this.run(async () => {
      const client = await this.connect();
      checkName(name);
      await client.deleteCollection(name);
      return { deleted: name };
    });
  }

  async upsert(collectionName, chunks, embeddings) {
    return this.run(async () => {
      if (!Array.isArray(chunks) || chunks.length === 0 || chunks.length > 64 || chunks.length !== embeddings?.length) {
        throw new InputError("每次导入同一文件的 1–64 个文档块，且向量数量必须一致");
      }
      const records = chunks.map((chunk, index) => {
        if (!chunk || typeof chunk !== "object") throw new InputError("文档块格式错误");
        const filename = checkText(chunk.metadata?.filename, "文件名", 200);
        const chunkIndex = chunk.metadata?.chunk;
        if (!Number.isInteger(chunkIndex) || chunkIndex < 0 || chunkIndex > 63) {
          throw new InputError("文档块序号必须是 0–63 的整数");
        }
        const id = createHash("sha256").update(`${filename}\0${chunkIndex}`).digest("hex").slice(0, 32);
        return {
          id,
          document: checkText(chunk.content, "文档内容", 16000),
          embedding: checkVector(embeddings[index]),
          metadata: { filename, chunk: chunkIndex },
        };
      });
      const filenames = new Set(records.map((record) => record.metadata.filename));
      if (filenames.size !== 1) throw new InputError("一次请求只能更新一个源文件");
      const collection = await this.getCollection(collectionName);
      const started = performance.now();
      const existing = await collection.get({ where: { filename: records[0].metadata.filename } });
      await collection.upsert({
        ids: records.map((record) => record.id),
        documents: records.map((record) => record.document),
        embeddings: records.map((record) => record.embedding),
        metadatas: records.map((record) => record.metadata),
      });
      const retained = new Set(records.map((record) => record.id));
      const stale = existing.ids.filter((id) => !retained.has(id));
      if (stale.length) await collection.delete({ ids: stale });
      await collection.refresh_index();
      const visible = await collection.query({
        queryEmbeddings: [records[0].embedding],
        where: { filename: records[0].metadata.filename },
        nResults: 1,
        include: [],
      });
      if (!visible.ids[0].length) throw new Error("写入后未能在 seekdb 向量索引中检索到文档");
      return { count: records.length, elapsedMs: performance.now() - started, ids: records.map((record) => record.id) };
    });
  }

  async search(collectionName, query, keyword, embedding) {
    return this.run(async () => {
      checkText(query, "查询", 1000);
      checkText(keyword, "全文关键词", 200);
      checkVector(embedding);
      const collection = await this.getCollection(collectionName);
      const started = performance.now();
      const result = await collection.hybridSearch({
        query: { whereDocument: { $contains: keyword }, nResults: 20 },
        knn: { queryEmbeddings: [embedding], nResults: 20 },
        rank: { rrf: {} },
        nResults: 10,
        include: ["documents", "metadatas", "distances"],
      });
      const results = result.ids[0].map((id, index) => ({
        id,
        document: result.documents[0][index],
        metadata: result.metadatas[0][index],
        distance: result.distances[0][index],
      }));
      return { results, elapsedMs: performance.now() - started };
    });
  }

  async close() {
    if (this.client) {
      const client = this.client;
      this.client = null;
      await client.close();
    }
  }
}
