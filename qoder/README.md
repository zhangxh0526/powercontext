# Qoder：科研实验续作助手

保存科研实验的当前进展与下一步，在重新打开应用后继续上次实验。Python 标准库后端通过真实 PowerContext 保存 decision Memory、按独立 Scope 召回，并展示记忆条目的身份和原文。页面包含空状态与错误状态，支持实验隔离和重启恢复。

主体设计、编码和调试使用 Qoder CLI；真实 HTTP 验收、浏览器截图与开发记录随源码提供。`site/` 是项目展示页，应用在本机运行。

## 运行

需要 Python 3.11+ 和已运行的 PowerContext 服务，应用本身没有额外 pip 依赖。完整准备步骤见 [运行与验证说明](RUN_AND_VERIFY.md)。

```bash
cp config.example.env .env            # 填入自己的 POWERCONTEXT_URL
set -a
. ./.env                             # 将已填写的配置导出给服务与验证脚本
set +a
./scripts/dev-server.sh start         # 启动，打开 http://127.0.0.1:8710/
python3 verification/verify_app.py              # 应用主流程（空状态/写入/召回/隔离/重启/错误状态）
python3 verification/verify_save_order.py       # 连续保存必须取回最后一次成功保存
python3 verification/verify_text_roundtrip.py   # 分号/字段名/引号/HTML/换行逐字往返
python3 scripts/demo-run.py                     # 端到端演示：创建→保存→召回→重启→再召回
./scripts/dev-server.sh stop          # 停止
```

- `RUN_AND_VERIFY.md`：安装、配置、启动、停止、验证说明，含具体初始数据、步骤、预期与实际结果。
- `EVIDENCE_INDEX.md`：`evidence/` 中真实请求与响应的索引，以及调用方提供的页面验收材料与失败记录的位置。
- `DEVELOPMENT_LOG.md`：开发过程记录（含踩坑与失败修复顺序），属过程材料，不作为验收结论。
- `development/QODER_SESSION.md`：从真实 Qoder 会话提取的可见消息、工具操作与执行回执。
- `site/`：项目展示站源文件，已由 Qoder Sites 发布为任何人可访问：
  [科研实验续作助手](https://experiment-recall-6htqqhz0les.qoder.website/)（项目简介、真实痛点、核心功能、架构、实际演示、运行与验证说明入口）。
  发布过程、站点 ID、部署状态与访问策略见 [SITE_PUBLICATION.md](SITE_PUBLICATION.md)。
- 目录结构：`backend/`（标准库后端与 PowerContext 客户端、进展文本编解码）、`web/`（页面）、
  `verification/`（真实 HTTP 验收脚本）、`scripts/`（开发服务与演示脚本）、`reference/powercontext.yaml`（契约）。

## 交付与验证

- Qoder Sites 站点处于 active、public；真实 Chrome 无痕窗口免登录访问首页和运行说明已通过，证据见 `evidence/public-site-20261030/`。
- 应用主流程 16 项、连续保存 5 项、文本往返 18 项真实 HTTP 断言全部通过；端到端演示验证进程重启后恢复同一条 entry。
- 实际 Chrome 页面验收覆盖保存、召回、特殊文本显示和重启恢复，证据见 `evidence/browser-ui-20261030/`。
- Qoder 账号 UID：`01a1876e-63bd-7a90-86af-11e66ea03c61`。开发材料包含真实 CLI 可见会话、执行回执和带事件编号的文字摘录截图。
- 公开站点提供项目展示；实际应用按运行说明在本机运行。未收集真实用户数量或提效统计。
