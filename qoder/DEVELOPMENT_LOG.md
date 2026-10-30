# 开发记录（过程材料，不作为验收结论）

本文件记录开发过程中的判断、踩坑与修复顺序。它属于开发过程材料，**不具备最终证明力**；
可复核的结果只在 `evidence/`（本项目真实 HTTP 记录）、`evidence/browser-ui-20261030/`（调用方页面验收）
与 `development/validation/`（调用方复核与失败记录）中，索引见 `EVIDENCE_INDEX.md`。

Qoder 账号 UID：`01a1876e-63bd-7a90-86af-11e66ea03c61`。可见会话记录见 `development/QODER_SESSION.md`；完整原始日志由所有者保存在 Linux PC 的 `qoder-records/`，不公开其中的内部推理或凭据。

## 第一阶段：按任务书实现应用与真实验证

- 先读 `reference/powercontext.yaml`（v1.2.0）再写接入代码：`/v1/scopes`、`/v1/memory/remember`、
  `/v1/context/prepare`、`/v1/memory/entries/list` 的请求体与响应体全部按契约实现，没有自造字段。
- 对真实服务探测后确认：该实例未配置向量模型，`search_modes` 只有 `auto/fts`，`memory_extraction` 关闭。
  FTS 会把整段中文连成一个词元，因此"上次进行到哪里，下一步是什么？"自由问题本身召回为 0 条。
  于是按任务书要求采用可检索锚点：保存文本以 `实验进展：` 开头，召回查询组合 `实验进展` ＋实验名称＋用户原问题。
- 解析 `/v1/context/prepare` 的组装 Markdown（`### Memory N` / `Entry: id=... version=...` / `>` 引用块）
  最初用了错误的行首假设，改为按真实返回文本逐行调试后才对齐 entry 身份与原文。
- 后端只用标准库（`ThreadingHTTPServer` + `urllib.request`），映射文件原子写入（临时文件 + `os.replace`）。
  静态文件走白名单，响应带 `X-Content-Type-Options: nosniff` 与 `Cache-Control: no-store`。
- 页面渲染全部用 `textContent` / `createElement`，没有 `innerHTML`；凭据只从进程环境读取，
  错误消息在返回前统一裁剪，绝不回显 token。

## 第二阶段：复核指出"连续保存召回旧进展"（真实失败 4/8）

- 复现到的失败：同一秒内连续两次保存，召回返回 FIRST。原始报告 `development/validation/initial-save-order-failure.json`
  为 8 组中 4 组失败（保留原样，未改写）。
- 第一个原因是时间戳只到秒，同秒两次保存的排序键相同，退化到按随机 entry id 比较。改为微秒时间戳后仍不够。
- 第二个原因更关键，也是最初没意识到的：`/v1/context/prepare` 返回的是**有界且按相关度/entry id 排序的截断候选集**。
  实测同一 Scope 保存 8 条时只返回 6 条，最新那条根本不在候选里，所以任何"只相信召回结果"的实现都可能取回旧记录。
- 修复方式：保存序号 `ordinal` 不取自本地计数，而是每次保存时读取该 Scope 的真实记忆清单
  （`/v1/memory/entries/list`）求最大序号后加一；召回时把 `prepare` 候选与记忆清单按 `entry_id` 合并，
  用 (序号, 保存时间, entry id) 排序取最新，并在响应与页面如实标注最新条目来自哪一路（`recall_sources.latest_from`）。
- 没有添加任何 sleep，也没有降低断言强度；调用方的复核脚本只在改变输出路径后运行，结果 8/8 SECOND。

## 第三阶段：复核指出文本被字段分隔符拆错，以及 entry 显示为问号

- 真实失败输入：`progress=完成基础测量`、`next_step=核对说明；下一步：重新测量`，
  召回却变成 `progress=完成基础测量；下一步：核对说明`、`next_step=重新测量`
  （`development/validation/save-order-and-text-review.json` 的 `text_roundtrip`）。
  根因是用 `；`/`进展：`/`下一步：` 拼接口径、再用非贪婪正则解析，无法区分用户文本与字段边界——这是设计缺陷，不是转义问题。
- 改用显式结构化编码：`实验进展：` + 单个 JSON 文档（`v/ordinal/saved_at/title/progress/next_step`），
  不再折叠用户空白。先用真实服务确认 JSON 文本能被 `/v1/memory/remember` 原样存入、
  被 `/v1/context/prepare` 与 `/v1/memory/entries/list` 原样取回，再落地实现。锚点仍是文本最前面的 `实验进展：`，
  FTS 召回行为不变。
- 旧记录的处理：`parse_progress_text` 只对**不带锚点**的文本返回 `None`（那本来就不是本应用写的记忆）。
  带锚点的旧"；"格式按 LEGACY 模式解析并标记 `codec=legacy-delimiter`；带锚点但两种格式都不匹配时标记
  `codec=undecodable`、`decodable=false`，召回状态仍是 `recalled` 并给出原文警告，页面按 PowerContext 原文显示。
  即"存在但无法解析"不会被说成"没有记忆"，也不会为重启后的数据做任何本地伪造。
- 保存结果显示 `entry ? / version ?` 的原因：前端读了 `payload.entry.entry_id`，而真实响应的身份在
  `payload.entry.citation.entry_id` / `.entry_version_id`。改为读取 `citation`，幂等写入（服务未返回 entry）时
  退回本地记录的上一次已确认 ID 并注明来源，不再用问号占位冒充已核对的 ID。
- 召回卡片增加"文本编码"行与无法解析时的警告行，让上面这些状态在页面上可见。

## 工具与流程上的踩坑（如实记录）

- 用 `pkill -f backend.server` 清理进程时，模式匹配到了自己的命令行，把当时所在的 shell 一起杀掉了两次。
  之后统一改用带转义的括号模式（`backend[.]server`）和 `scripts/dev-server.sh` 的 pid 文件管理。
- 验收脚本最初用中文实验名生成 id 并拼进 URL 路径，触发 `UnicodeEncodeError`；改为 slug 只保留 ASCII，
  路径段再按需 percent-encode。
- 验收脚本自身出过几个错误：没把 `ASSISTANT_PORT` 传给子进程、数据目录在打开子进程日志前未创建、
  父进程复用了子进程的 stdout 句柄、日志句柄未关闭、`write_evidence` 里读了不存在的 `at` 键。均已修复。
- 我曾一次性用字符串替换批量改测试代码，把多处 f-字符串改坏（出现 `{quote(experiment_a[chr(39)+chr(39)])}`），
  另一个辅助函数改动抛出 `TypeError: replace expected at least 2 arguments`。改为逐处编辑并立刻编译验证。
- 本阶段一度加入过基于 CDP 的页面检查脚本；按复核要求，验证只允许文件/命令行/真实 HTTP，
  这些脚本已从 `verification/` 移除且从未在本轮执行，页面验收由调用方用浏览器完成。

## 本轮结束时的交付状态

- 三套真实 HTTP 回归与端到端演示都在最终代码上重跑并通过：`verify_app.py` 16 项、`verify_save_order.py` 5 项、
  `verify_text_roundtrip.py` 18 项、`scripts/demo-run.py`（含重启进程后同一 entry 恢复），退出码均为 0。
  `verify_powercontext.py` 独立服务验证同样 `PASS`。
- 站点 `site/`（`index.html`、`run.html`、`styles.css`、`assets/application.jpg`）已用 `python3 -m http.server`
  加 `urllib` 逐条检查内部链接，全部 200；未使用任何浏览器或桌面自动化。站点不含本地 PowerContext 地址与 token。
- 文档：`RUN_AND_VERIFY.md`（安装→启动→停止→验证＋初始数据/步骤/预期/实际）、`EVIDENCE_INDEX.md`（证据索引）、
  `config.example.env`（占位配置）、`.gitignore`（忽略 `.env`、`data/`、`runtime/`，保留 `evidence/`）、
  `README.md` 增加应用入口一节（原有内容未改写）。
- 本机开发服务仍在 `http://127.0.0.1:8710` 运行，方便调用方复核修复后的页面；停止用 `./scripts/dev-server.sh stop`。
- 本阶段未发布站点、未推送 Git、未修改任何账号权限。

## 已知边界与剩余事项

- 站点尚未发布，未推送 Git，未修改任何账号权限；`site/` 目前是本地静态展示，链接已在纯静态服务器上逐条检查。
- 公开站点不含后端，实际体验需按 `RUN_AND_VERIFY.md` 在本机进行。
- 该开发阶段结束时，修复后的页面仍需调用方重新用浏览器复核。
  后续真实 Chrome 验收已完成保存、召回、特殊文本显示和重启恢复，最终材料见 `evidence/browser-ui-20261030/`。
- 未收集任何真实用户使用数据或提效数字，相关位置留空。
