# 证据索引

日期：2026-10-30（Asia/Shanghai）。应用脚本使用真实 HTTP 请求；页面验收由 Codex 通过已连接的 Chrome 执行。所有演示数据均为虚构实验。

## HTTP 验收

| 文件 | 结论 | 覆盖行为 |
| --- | --- | --- |
| `evidence/app-20261030T032145Z/app-acceptance.json` | PASS，16 项 | 空状态、真实写入、召回、实验隔离、进程重启恢复、输入拒绝和错误状态 |
| `evidence/save-order-20261030T032846Z/save-order-regression.json` | PASS，5 项 | 同秒连续保存、超过候选数量的历史、最新记录补全与重启恢复 |
| `evidence/text-roundtrip-20261030T032844Z/text-roundtrip.json` | PASS，18 项 | 分隔符、字段名、引号、HTML 样式文本、换行、旧记录读取及精确身份 |
| `evidence/demo-run-20261030T032654Z/demo-run.json` | PASS | 创建、写入、召回、重启应用进程后恢复同一 entry |

服务不可用场景通过为独立应用进程配置实际不存在的服务地址验证；接口错误场景使用真实服务返回的 Scope 不存在响应。它们证明本地应用的错误处理，不代表外部平台验收。

运行命令和具体输入输出见 [运行与验证说明](RUN_AND_VERIFY.md)。`reference/powercontext.yaml` 是接入契约快照。

## 浏览器验收

- `evidence/browser-ui-20261030/acceptance.json`：真实 Chrome 页面创建实验、空状态、保存、召回、HTML 按字面显示、分隔符与引号保留、应用重启后同一 Scope 和 entry 恢复。
- `evidence/browser-ui-20261030/restart-recall-page.md`：重启后实际可见页面结构。
- `evidence/browser-ui-20261030/application.jpg`：实际应用截图，展示站复用同一张图片。
- 核对的 entry：`mem_ent_52594825f08042b38a75430ce8af88a9`；version：`mem_ver_6c284b117a804794b5ce128f1a9f94be`。

## Qoder 开发与复核记录

- [可见 Qoder 会话记录](development/QODER_SESSION.md)：真实可见助手消息、工具操作和执行回执。
- [开发过程记录](DEVELOPMENT_LOG.md)：需求拆解、实现和调试过程。
- `development/validation/initial-save-order-failure.json`：实际观察到的原始失败，8 组中 4 组返回旧记录。
- `development/validation/save-order-and-text-review.json`：保存顺序修复后 8 组通过，并保留文本分隔缺陷的实际输入和输出。
- 其余完整原始 Qoder 日志保留在所有者 Linux PC 的 `/home/legend/seekdb-competition/qoder-records/`，不作为应用运行依赖。

开发过程中的失败与调试记录用于追溯，不作为通过结果。项目没有真实用户数量或提效统计，也未据此作出效果主张。
