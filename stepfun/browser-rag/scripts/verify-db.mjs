import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { KnowledgeBase } from "../src/database.mjs";

const suffix = Date.now();
const output = resolve("artifacts", `db-${suffix}`);
const database = new KnowledgeBase();
const main = `verify_main_${suffix}`;
const other = `verify_other_${suffix}`;
const vector = (index, second = -1) => Array.from({ length: 512 }, (_, i) => Number(i === index) + Number(i === second));
const chunk = (filename, content) => [{ content, metadata: { filename, chunk: 0 } }];
const checks = [];

try {
  await database.create(main);
  await database.upsert(main, chunk("fts.md", "全文索引通过倒排结构提升查询效率"), [vector(0)]);
  await database.upsert(main, chunk("hybrid.md", "向量检索和全文索引的混合检索"), [vector(0, 1)]);
  await database.upsert(main, chunk("other.md", "完全不同的实验记录"), [vector(1)]);
  let result = await database.search(main, "如何提升查询效率？", "全文索引", vector(0));
  assert.equal(result.results.length, 3);
  assert.equal(result.results[0].metadata.filename, "fts.md");
  checks.push("database hybrid search returns full-text and vector candidates in rank order");

  await database.upsert(main, chunk("fts.md", "更新后的全文索引检索说明"), [vector(0)]);
  result = await database.search(main, "检索说明", "全文索引", vector(0));
  assert.equal(result.results.find((item) => item.metadata.filename === "fts.md").document, "更新后的全文索引检索说明");
  checks.push("same source file updates rather than duplicating content");

  await database.upsert(main, [
    { content: "第一块全文索引", metadata: { filename: "multi.md", chunk: 0 } },
    { content: "第二块向量检索", metadata: { filename: "multi.md", chunk: 1 } },
  ], [vector(0), vector(1)]);
  await database.upsert(main, chunk("multi.md", "仅保留更新后的全文索引"), [vector(0)]);
  const collection = await database.getCollection(main);
  assert.equal((await collection.get({ where: { filename: "multi.md" } })).ids.length, 1);
  checks.push("shorter replacement removes stale trailing chunks");

  await database.create(other);
  await database.upsert(other, chunk("other-collection.md", "其他集合的全文索引"), [vector(0)]);
  result = await database.search(other, "全文索引", "全文索引", vector(0));
  assert.equal(result.results.length, 1);
  assert.equal(result.results[0].metadata.filename, "other-collection.md");
  checks.push("collections isolate stored documents and search results");

  await assert.rejects(database.upsert(main, chunk("invalid.md", "无效向量"), [[1, 0]]), /512/);
  checks.push("invalid vector dimensions are rejected before writing");
  await database.close();

  const restarted = new KnowledgeBase();
  try {
    result = await restarted.search(main, "检索说明", "全文索引", vector(0));
    assert.equal(result.results.find((item) => item.metadata.filename === "fts.md").document, "更新后的全文索引检索说明");
    checks.push("a new database client recovers the updated document after restart");
    assert.ok((await restarted.list()).collections.includes(other));
    await restarted.remove(main);
    await restarted.remove(other);
    checks.push("verification collections are removed after successful checks");
  } finally {
    await restarted.close();
  }
  await mkdir(output, { recursive: true });
  await writeFile(resolve(output, "verification.json"), JSON.stringify({ success: true, checks }, null, 2) + "\n");
  console.log(JSON.stringify({ success: true, checks, evidence: resolve(output, "verification.json") }, null, 2));
} finally {
  await database.close();
}
