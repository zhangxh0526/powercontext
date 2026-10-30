# Qoder Sites 发布记录

本站点由 Qoder CLI 的 Sites 流程从本地 `site/` 目录发布，部署与访问策略来自工具的真实读回。发布后另由 Codex 使用实际 Chrome 无痕窗口验收。

## 最终结果

| 项 | 值 |
| --- | --- |
| 公开网址 | https://experiment-recall-6htqqhz0les.qoder.website |
| host | `experiment-recall-6htqqhz0les.qoder.website` |
| 地址前缀 / 后缀 | `experiment-recall` + `-6htqqhz0les.qoder.website`（owner 固定后缀） |
| Project ID | `01a18bbb-983e-7078-9551-5d4ae2ba7b36` |
| Site ID | `01a18bbb-9840-76f5-a34b-52c8aa319ca2` |
| 站点名称 | 科研实验续作助手 |
| 访问策略 | `public`（任何人可访问），`auth_integration_status: not_required`，`include_project_members: false`，`rule_count: 0` |
| 部署状态 | `published: true`，发布 Operation `succeeded` 且 `committed: true`，`readiness: publication_completed`，`lifecycle: active`，`governance_status: normal`，`hasPublishedContent: true` |
| 源码版本 | 分支 `qoder-solution`，commit `5ff79776` |
| 账号 UID | `01a1876e-63bd-7a90-86af-11e66ea03c61` |

真实 Chrome 无痕窗口无需登录即可打开首页和 `/run.html`。截图、可见页面记录和免认证 HTTP 检查保存在 `evidence/public-site-20261030/`；应用交互与重启恢复的页面证据见 `evidence/browser-ui-20261030/`。

## 发布内容

`prepare_site` 只打包 `webDirectory: "site"`，上传的文件为：

- `site/index.html`
- `site/run.html`
- `site/styles.css`
- `site/assets/application.jpg`

未包含 `.env`、`config*.env` 真实凭据、`data/`、`runtime/`、原始 jsonl 会话记录、`backend/`、`web/` 应用代码。站点为纯静态展示，不含本地 PowerContext 服务地址（发布后正文中检索 `18081` 无匹配）、不含任何 token 值，仅出现配置项名称 `POWERCONTEXT_URL` / `POWERCONTEXT_TOKEN` / `POWERCONTEXT_SCOPE` 与文档中的本机默认地址示例。

## 过程记录（CLI 宿主）

宿主为 Qoder CLI Sites 操作，命令形式为
`qodercli-1.1.81 sites --session "c78d3cf9-c21d-4eea-a0ab-2c8bb56e844e" <operation> --input/--input-file`；凭据由宿主提供，未在本地读取或传递。每次只执行一条 Sites 命令。

1. **确认目标，避免重复项目。** `list_sites` 在该项目目录下返回 0 条已验证站点，因此按新建站点处理，没有创建第二个项目。
2. **冻结草稿。** `prepare_site`：
   - `actionId`：`a7feafa7-28db-4dbc-af62-9f5179b3cef4`（后续所有步骤复用同一 action）
   - `projectRoot`：`/home/legend/seekdb-competition/qoder`，`webDirectory`：`site`
   - `displayName`：`科研实验续作助手`，`slug`：`experiment-recall`，`spa`：`false`
   - 返回 `projectId 01a18bbb-983e-7078-9551-5d4ae2ba7b36`、`siteId 01a18bbb-9840-76f5-a34b-52c8aa319ca2`、
     `verificationOperationId 01a18bbb-9c14-7bb9-8206-7a8a145eac59`、`descriptorStatus: "pending"`。
3. **发布前状态读回。** `get_publish_status`：`artifactReady: true`、`dependenciesReady: true`、`blockers: []`、`canPublish: true`、`preparation.state: ready`、验证 Operation 成功。
4. **发布。** `publish_site`（同一 action）→ `publishOperationId 01a18bbc-1b67-7791-8f7b-3298ce7ce480`；随后按返回节奏重复 `get_publish_status`，得到 `published: true`、`operation.committed: true`、`readiness: publication_completed`、`site.url https://experiment-recall-6htqqhz0les.qoder.website`，初始 `access_mode: private`。
5. **改为任何人可访问。**（公开发布已由用户明确授权）先以 `get_site_access` 读取当前策略与可用模式（`private` / `selected` / `public`，`public` 需要显式确认），再 `update_access_policy`（`requestId 74445f43-a9ef-4266-9050-8ecb2562b142`、`mode: "public"`、`confirmPublic: true`），返回
   `{"mode":"public","include_project_members":false,"rule_count":"0","auth_integration_status":"not_required"}`。
6. **云端复核。** `get_site` 读回 `access_mode: public`、`hasPublishedContent: true`、project `status: active`。
7. **可达性与内容一致性（HTTP，非浏览器）。** 对公开地址执行真实 HTTPS 请求：

   | 路径 | 状态 | Content-Type | 字节数 |
   | --- | --- | --- | --- |
   | `/` | 200 | text/html; charset=utf-8 | 66779 |
   | `/run.html` | 200 | text/html; charset=utf-8 | 69163 |
   | `/styles.css` | 200 | text/css; charset=utf-8 | 3641 |
   | `/assets/application.jpg` | 200 | image/jpeg | 253015 |

   `styles.css` 与 `application.jpg` 字节数与本地源文件一致。HTML 响应比本地源文件大，是因为平台注入了一个 `data-qoder-watermark="v1"` 脚本；源文件正文原样包含在响应中，站点标题、"上次进行到哪里"、PowerContext 说明与仓库链接均可在响应正文中检索到。
8. **本地描述文件。** 发布后再次以同一 action 和完全相同的输入调用 `prepare_site`（不重新打包、不上传、不重复发布），把先前的 `descriptorStatus: "pending"` 恢复为 `written`，
   生成 `/home/legend/seekdb-competition/qoder/.科研实验续作助手.qoder.site`，返回的 Project/Site ID 与上表一致。

## 后续维护说明

- 这是已完成的 action：对其重复 `publish_site` 只会返回既有发布结果，不会重新部署。要发布新内容，需要在同一 `projectId` 下使用新的 `actionId` 重新 `prepare_site`（`webDirectory: "site"`）。
- 未暂停、未删除站点，未修改已验收的 `backend/`、`web/`、`verification/` 代码，未停止本机 PowerContext 服务。
