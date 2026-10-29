# 拾光知识库：浏览器本地推理 + seekdb 混合检索

导入 `.txt` / `.md` 文档后，浏览器使用 Transformers.js 在本地生成 512 维向量，Node.js 后端把向量和原文写入 seekdb。查询由浏览器向量化，再由 seekdb 完成全文与向量混合检索，不在客户端拼接两路结果。

## 环境与依赖

- Node.js 22 或更新版本，npm，Bash，Chrome / Edge 最新版。
- 可连接的 seekdb server。默认连接本机 `127.0.0.1:12881` 的 `test` 数据库；使用专用测试实例，不要使用生产数据库。
- 三种精度的模型权重合计 166,360,146 字节（约 158.7 MiB），另有少量 tokenizer 配置。模型文件和 `node_modules` 不提交到 Git。

安装依赖：

```bash
npm ci
```

项目 `.npmrc` 不自动安装未使用的服务端 embedding peer，并跳过 Node 端 ONNX 的安装脚本。实际推理使用浏览器端 `onnxruntime-web`，数据库通过 `seekdb` 的 server 模式连接，不需要原生嵌入式绑定或 libaio。

## 准备模型

在能够访问 Hugging Face 的机器上执行：

```bash
bash scripts/download-models.sh
```

脚本从 `Xenova/bge-small-zh-v1.5` 的固定 revision 下载 FP32、FP16、INT8 及 tokenizer 文件到 `public/models/`。如果下载机器与运行机器不同，复制这个目录到运行机器即可。应用只读取本地模型，已设置 `allowRemoteModels=false`，运行阶段不请求 Hugging Face 或云推理 API。

模型来源：<https://huggingface.co/Xenova/bge-small-zh-v1.5>；原始模型为 `BAAI/bge-small-zh-v1.5`。revision 为 `75c43b069aac4d136ba6bc1122f995fedcfd2781`。模型许可见上游仓库。

## 启动

先启动 seekdb server，再按需设置连接参数。密码只放在当前 shell 的环境变量中，不要写入仓库：

```bash
export SEEKDB_HOST=127.0.0.1
export SEEKDB_PORT=12881
export SEEKDB_USER=root
export SEEKDB_PASSWORD='replace-with-your-local-password'
export SEEKDB_DATABASE=test
export PORT=8080
npm start
```

打开 `http://127.0.0.1:8080`。在另一台机器上查看时，可使用 SSH 端口转发：

```bash
ssh -N -L 8080:127.0.0.1:8080 legend@192.168.1.132
```

然后访问 Mac 上的 `http://127.0.0.1:8080`。模型推理在打开网页的浏览器设备上执行，不在 Linux 服务器上执行。

## 功能验证样例

1. 点击“加载演示语料”，再点击“本地向量化并导入”。首次会加载模型，之后显示实际后端、精度和加载/预热耗时。
2. 等待导入四篇文档成功。写入包含索引刷新与向量可见性检查，未成功时会显示错误。
3. 查询输入 `怎样提升全文检索效率？`，全文关键词输入 `全文索引`，点击检索。
4. 应看到 `fts.md` 中关于倒排结构的原文；数据库也可能返回其他向量候选。
5. 新建一个知识库，导入文档后切换集合，确认只检索当前集合。重启 Node.js 服务后，保持相同集合名，再次检索确认数据仍可恢复。
6. 没有 WebGPU 或 FP16 不可用时，会尝试 WebGPU FP32；无法使用 WebGPU 时降级 WASM + INT8。页面始终显示实际后端，不把请求的后端当作已生效的后端。

单文件最大 1 MiB，每次更新同一文件的 1–64 个分块。文件名和序号决定文档 ID，更新后删除失效的旧分块。知识库名称限 1–40 个英文字母、数字或下划线。删除知识库需要页面确认。

## 真实验证

```bash
npm run check
npm run verify:db
npm run verify:browser -- --wasm
npm run verify:browser
npm run evaluate
```

- `verify:db` 使用真实 seekdb 创建独立 collection，验证混合排序、更新、旧分块清理、集合隔离、维度校验及重启恢复，成功后清理这些 collection。
- `verify:browser -- --wasm` 强制验证 WASM + INT8；不带参数时优先验证 WebGPU，若实际不可用则记录真实降级路径。
- `evaluate` 在浏览器中分别运行 FP32、INT8，并尝试 WebGPU FP32，输出模型文件体积、分阶段延迟和固定小语料的 Recall@2 / nDCG@5。它不是云端推理，也不将预计算向量用于应用检索。
- 首次使用 Playwright 时，可用 `CHROME_PATH=/path/to/chrome npm run verify:browser -- --wasm` 指定已安装的 Chrome。脚本启动的应用端口是 `18082`，量化评测使用 `18083`，请保持空闲。

真实验证输出保存在本机 `artifacts/`，不会自动提交。交付中的 `evidence/` 保存去除凭据后的结果和演示截图。技术实现与实测口径见 `TECHNICAL_NOTES.md`。
