# 技术说明

## 架构与技术结合

应用在浏览器中使用 `@huggingface/transformers 4.3.2` 执行 embedding 推理，模型为公开 `Xenova/bge-small-zh-v1.5`，固定 revision `75c43b069aac4d136ba6bc1122f995fedcfd2781`，输出 512 维向量。采用 CLS pooling、归一化和 BGE 中文查询前缀。文档和查询都在当前浏览器设备上计算，没有云端推理 API；模型从固定 URL 下载后由本地静态服务提供，`allowRemoteModels=false`。

Node.js 22 后端通过 `seekdb 1.3.0` 的 server 模式写入向量、原文和元数据。collection 使用 IK smart 全文索引、cosine HNSW 向量索引，`hybridSearch()` 在 seekdb 内执行两路检索和 RRF 排名融合，客户端不做结果拼合。每次写入后调用 `refresh_index()`，再实际执行向量查询确认新内容可见。

同源文件按文件名和分块序号生成确定性 ID；更新文件后移除失效的旧分块。collection 隔离知识库，创建、切换、删除和重启后数据恢复均通过真实数据库验证。服务不安装或使用原生嵌入式绑定。最终 `npm ci` 成功，`npm audit` 为 0 vulnerabilities。

## 量化、后端与实测

量化使用 Transformers.js `q8`，实际加载 `onnx/model_quantized.onnx`（INT8）；对照组加载 `onnx/model.onnx`（FP32）。评测使用同一 Chrome、六篇人工标注的小型中英文语料、五个查询、相同批大小，所有向量均在浏览器中实时计算。Recall@2 和 nDCG@5 基于人工相关文档标注，不代表通用模型质量。原始数据见 `evidence/model-metrics.json`。

| 配置 | 运行文件总字节数（含 tokenizer） | 加载/预热 | 文档批次耗时（4 块 / 2 块） | 预热后查询中位耗时 | Recall@2 | nDCG@5 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| WASM + FP32 | 95,401,750 | 1,578.6 ms | 267.3 / 70.0 ms | 36.75 ms | 1.0 | 1.0 |
| WASM + INT8 | 24,560,715 | 1,063.8 ms | 147.7 / 41.3 ms | 22.50 ms | 1.0 | 1.0 |
| WebGPU + FP32 | 95,401,750 | 3,390.5 ms | 6,254.5 / 1,430.6 ms | 751.25 ms | 1.0 | 1.0 |

包含 tokenizer 的模型运行文件总量压缩比为 **3.884×**。在这个小测试集上，INT8 没有降低 Recall@2 或 nDCG@5；预热后查询中位耗时比 FP32 低约 38.8%。这些数字仅适用于本次设备、浏览器和语料，不能外推为普遍结论。

自动后端优先尝试 WebGPU FP16，当前运行时明确报告不支持该精度后使用 WebGPU FP32。无法使用 WebGPU 时降级 WASM + INT8。实测适配器为 **SwiftShader**，所以上述 WebGPU 较慢的结果是软件后端表现，**不能作为真实硬件 GPU 加速效果的结论**。真实硬件 GPU 与 FP16 的性能未验证。

## 验证材料

- `evidence/database-verification.json`：混合检索、更新、旧分块清理、集合隔离、向量维度校验、重启恢复和测试数据清理。
- `evidence/wasm-verification.json`、`evidence/wasm-application.png`：实际 WASM + INT8 的文档导入、混合检索、集合切换、重启恢复和清理。
- `evidence/webgpu-verification.json`、`evidence/webgpu-application.png`：实际 WebGPU + FP32 的同一主流程。
- `evidence/model-metrics.json`：量化、后端、原始延迟、适配器和人工标注小语料的质量结果。

复现命令与配置见 `README.md`。不把模型加载成功、静态页面或单独的数据库连接当作完整功能验证。
