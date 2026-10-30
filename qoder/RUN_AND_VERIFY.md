# 运行与验证说明

科研实验续作助手：Python 3.11+ 标准库后端 + 原生 HTML/CSS/JS 前端，记忆写入与召回全部走真实 PowerContext。
本页给出安装、配置、启动、停止和验证；公开站点展示版见 `site/run.html`。

## 1. 环境要求与安装

- Python 3.11 或更高版本（`python3 --version`）。应用只用标准库，**没有 pip 依赖需要安装**。
- 一个可访问的 PowerContext 服务（OpenAPI v1.2.0），需要这些端点：
  `/health/ready`、`/v1/scopes`、`/v1/memory/remember`、`/v1/context/prepare`、`/v1/memory/entries/list`。
- bash、curl（可选，用于演示脚本）。

```bash
git clone --branch qoder-solution https://github.com/zhangxh0526/powercontext.git
cd powercontext/qoder
python3 --version          # 需要 >= 3.11
```

## 2. 配置

```bash
cp config.example.env .env   # .env 已在 .gitignore 中，不会被提交
```

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `POWERCONTEXT_URL` | 是 | PowerContext 服务地址，必须是 http/https URL。未设置时默认 `http://127.0.0.1:8000`，非 http/https 会启动即报错。 |
| `POWERCONTEXT_TOKEN` | 否 | 服务启用鉴权时设置。只作为请求头发送，不写日志、不写证据、不出现在任何响应里。 |
| `POWERCONTEXT_SCOPE` | 否 | **只**可作为"默认实验"的 Scope 复用来源；留空时每个实验都创建并持久保存自己的 Scope，绝不让所有实验共用一个 Scope。 |
| `ASSISTANT_HOST` / `ASSISTANT_PORT` | 否 | 应用监听地址与端口，默认 `127.0.0.1:8710`。 |
| `ASSISTANT_DATA_DIR` | 否 | 实验与 Scope 映射目录，默认项目下 `data/`。本地文件还包含保存时的表单元数据；召回始终读取 PowerContext，不使用这些本地字段兜底。 |

## 3. 启动

前台运行（最直接，Ctrl-C 停止）：

```bash
cd powercontext/qoder
export POWERCONTEXT_URL='http://127.0.0.1:8000'  # 改为已有服务的地址
python3 -m backend.server
# 打开 http://127.0.0.1:8710/
```

后台服务脚本（自动读取本地 `.env`）：

脚本在未导出 `POWERCONTEXT_URL` 时读取 `.env`；如果已经导出该变量，则使用完整的进程环境配置。`.env` 中的监听地址和端口同时用于启动、状态检查与停止。

```bash
./scripts/dev-server.sh start     # 启动，并等待 /api/health 就绪
./scripts/dev-server.sh status    # 进程与健康状态
./scripts/dev-server.sh logs 50   # 最近 50 行日志
./scripts/dev-server.sh restart   # 重启（保留同一数据目录与同一 Scope）
./scripts/dev-server.sh stop      # 停止
```

pid 在 `runtime/dev-server.pid`，日志在 `runtime/dev-server.log`（两者都不提交）。

## 4. 停止

- 前台：`Ctrl-C`。
- 脚本：`./scripts/dev-server.sh stop`（先 SIGTERM，10 秒未退出才 SIGKILL，并清理 pid 文件）。
- 停止不会删除 `data/experiments.json`，重新启动后实验与 Scope 映射仍然有效。

## 5. 真实 HTTP 验证

应用验收脚本会启动独立应用进程、发送真实 HTTP 请求并断言结果，失败时以非零状态退出，把真实请求与响应写入 `evidence/`。`verify_powercontext.py` 单独验证已运行的 PowerContext 服务。
只用命令行与 HTTP，**不启动任何浏览器或桌面自动化**。

```bash
export POWERCONTEXT_URL='http://127.0.0.1:8000'  # 改为已有服务的地址

python3 verify_powercontext.py                 # 独立验证 PowerContext 服务本身
python3 verification/verify_app.py             # 应用主流程：空状态/写入/召回/隔离/重启/错误状态
python3 verification/verify_save_order.py      # 连续保存回归：最新一次成功保存必须被召回
python3 verification/verify_text_roundtrip.py  # 文本往返回归：分号、字段名、引号、HTML、换行逐字一致
```

命令行端到端演示（需要先用 `./scripts/dev-server.sh start` 启动应用）：

```bash
python3 scripts/demo-run.py                    # 创建 → 保存 → 召回 → 重启进程 → 再次召回，退出码即结论
```

单独验证服务可用性与能力（无凭据时也适用）：

```bash
python3 verify_powercontext.py
```

## 6. 验证样例：具体初始数据、操作步骤、预期与实际结果

### 初始数据（虚构）

| 字段 | 值 |
| --- | --- |
| 实验名称 | `演示实验 RDMA 尾延迟` |
| 当前进展 | `完成基础测量；备注 <b>原样文本</b> 和 "控制组A"` |
| 下一步 | `核对说明；下一步：固定线程数后重新测量 P99 延迟` |
| 召回问题 | `上次进行到哪里，下一步是什么？` |

这组输入同时包含中文分号、字段名 `下一步：`、双引号与 HTML 样式标签，用来检验字段边界不会被用户文本带偏。

### 操作步骤

1. `./scripts/dev-server.sh start`，确认 `/api/health` 返回 `state=ready`。
2. `POST /api/experiments`（标题见上）→ 期望 201，响应内含为该实验新建的独立 `scope_id`。
3. `POST /api/experiments/<id>/recall`（在保存之前）→ 期望 200 且 `state=no_memory`（真实空状态，不是示例文本）。
4. `POST /api/experiments/<id>/save` → 期望 200、`state=saved`，且 `entry.citation.entry_id` 以 `mem_ent_` 开头、`entry.citation.entry_version_id` 以 `mem_ver_` 开头（来自真实 `/v1/memory/remember` 响应）。
5. `POST /api/experiments/<id>/recall` → 期望 200、`state=recalled`，`latest.parsed.progress` 与 `latest.parsed.next_step` 与输入逐字相同。
6. `./scripts/dev-server.sh restart` 后再次召回 → 期望恢复到**同一条 entry**，字段仍逐字一致。
7. 破坏性检查（脚本内完成）：把映射里的 `scope_id` 改成不存在的值 → 期望 502 `api_error`；把 `POWERCONTEXT_URL` 指向空端口 → 期望健康检查 `service_unavailable`、保存与召回 503，绝不显示成功。

### 实际结果

命令行端到端演示 `scripts/demo-run.py`（证据：`evidence/demo-run-20261030T032654Z/demo-run.json`，结论 `PASS`，退出码 0）：

```text
scope_id                     scp_73dng2c0cvnf7sstts1swszkwx
写入响应 entry ID            mem_ent_631e36769cad4fd09f6de0deb4f389a7
写入响应 version ID          mem_ver_f61cdac3d72446b4bc9dec8cb04d8261
召回进展                     完成基础测量；备注 <b>原样文本</b> 和 "控制组A"
召回下一步                   核对说明；下一步：固定线程数后重新测量 P99 延迟
召回文本编码                 json-v1
重启进程后再次召回           同一条 mem_ent_631e36769cad4fd09f6de0deb4f389a7，字段逐字一致
```

自动化断言输出（当前实现的实际结果，逐条对应 `evidence/` 内的真实响应）：

```text
$ python3 verification/verify_app.py             → 验收结论：PASS（16 项，退出码 0）
PASS  应用启动且 PowerContext 就绪
PASS  新实验首次召回显示无记忆
PASS  写入 PowerContext 返回 decision Memory 条目
PASS  召回返回实验名称、进展与下一步
PASS  不同实验绑定不同 Scope
PASS  实验隔离：A 的召回不含 B 的记忆
PASS  实验隔离：B 只召回自己的进展
PASS  重启后实验与 Scope 映射仍然存在
PASS  重启后同一 Scope 恢复已保存进展与下一步
PASS  空的进展或下一步被校验拒绝且不写入
PASS  超出 8192 字节的记忆被拒绝而非截断写入
PASS  两次被拒绝的请求没有产生新的记忆条目
PASS  接口错误显示为 api_error 而非成功召回
PASS  服务不可用显示 service_unavailable
PASS  服务不可用时保存请求不伪装成功
PASS  服务不可用时召回请求不伪装成功
证据：evidence/app-20261030T032145Z/app-acceptance.json

$ python3 verification/verify_save_order.py      → 连续保存回归结论：PASS（退出码 0）
PASS  同一秒连续保存 8 组全部召回 SECOND：失败 0 组
PASS  单个实验 12 次保存后召回最新一次（候选集截断不影响结果）
PASS  连续 24 次保存内每次召回都恢复最新一次成功保存
PASS  相关度召回漏掉最新保存时由 Scope 记忆清单补全恢复
PASS  应用重启后仍恢复最后一次成功保存
证据：evidence/save-order-20261030T032846Z/save-order-regression.json

$ python3 verification/verify_text_roundtrip.py  → 文本往返回归结论：PASS（18 项，退出码 0）
PASS  保存响应在 entry.citation 中返回真实 entry/version ID
PASS  中文分号与字段名不再拆错字段（progress 逐字一致）
PASS  中文分号与字段名不再拆错字段（next_step 逐字一致）
PASS  召回条目使用结构化编码且可解析
PASS  召回使用保存响应返回的同一 entry 身份
PASS  实验名称中的锚点与字段名完整保留
PASS  引号、HTML 样式文本与换行在 progress 中逐字保留
PASS  JSON 样式与反斜杠在 next_step 中逐字保留
PASS  PowerContext 原文仍带可检索的实验进展锚点
PASS  旧分隔符记录仍被识别为已召回而非无记忆
PASS  旧分隔符记录的进展与下一步仍按旧格式解析
PASS  新保存的序号在旧记录之后且成为最新条目
PASS  带锚点但无法解析的记录不被当作无记忆
PASS  不可解析记录标记为 undecodable 并给出原文警告
PASS  不可解析记录按 PowerContext 原文完整保留
PASS  不可解析记录不影响最新成功保存的召回
PASS  重启后含中文分号的进展与下一步仍逐字一致
PASS  重启后引号、HTML 样式文本与换行仍逐字一致
证据：evidence/text-roundtrip-20261030T032844Z/text-roundtrip.json

$ python3 verify_powercontext.py                 → PASS: PowerContext returns the saved
  experiment progress and next step（独立服务验证，退出码 0）
```

页面交互与截图由项目验收方在自己的浏览器中完成，结果见 `evidence/browser-ui-20261030/`（说明与索引见 `EVIDENCE_INDEX.md`）。

## 7. 已知边界

- 未配置向量模型时服务只有 FTS 词元匹配，中文自由问题本身可能不命中；因此查询始终组合锚点 `实验进展`＋实验名称＋用户问题。
- `max_bytes` 与 `assembly.sections` 的候选集是有界的，最新保存可能不在其中；应用用同一 Scope 的 `/v1/memory/entries/list` 补全比较，召回来源会在页面与响应中如实标注。
- 保存和召回会读取当前 Scope 的进展清单，工作量随该实验的历史条目数增长；尚未对大规模历史记录作性能验证。
- 单条记忆文本受服务 8192 UTF-8 字节限制约束：超限时后端拒绝写入并返回 502 `api_error`（`code=text_too_long`），绝不截断后当作保存成功。实测响应：
  `记忆文本超出 PowerContext 上限 8192 UTF-8 字节（当前 8445 字节）`。
- `site/` 是纯静态展示，没有公开后端；体验需按上面的说明在本机完成。
