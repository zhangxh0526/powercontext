# 记忆 NPC（Dify + PowerContext）

这是一个两轮对话的记忆互动应用。Dify 负责输入、HTTP 调用、模型编排和输出；PowerContext 负责保存玩家选择并在后续问题中召回相关上下文。

## 文件

- `dify/memory_npc.yml`：从 Dify Cloud 导出的工作流 DSL（advanced-chat，版本 0.7.0），服务地址、Scope 和密钥值均为可替换配置。
- `dify/verify_powercontext.py`：只使用 Python 标准库，创建演示 Scope，写入“前往森林并带上地图”，再发起后续查询验证召回。
- `dify/evidence/`：Dify Cloud 实际两轮对话、节点 HTTP 响应、空 Scope 基线和截图。

## 运行准备

1. 启动 PowerContext，确认 `http://127.0.0.1:8000/health/live` 可访问。
2. 执行真实写入和召回验证：

   ```bash
   python3 dify/verify_powercontext.py
   ```

   脚本会输出 `POWERCONTEXT_SCOPE=<实际 scope_id>`、记忆写入响应和召回结果。成功时末尾为 `PASS: the next query recalls both forest and map`。

3. 如果服务启用了鉴权，只在本地环境变量中配置 token，不要写入 DSL：

   ```bash
   export POWERCONTEXT_TOKEN='replace-with-your-own-token'
   ```

## 导入和配置 Dify

1. 在 Dify 中导入 `dify/memory_npc.yml`。DSL 声明了 OpenAI marketplace 依赖；在 Dify 中配置可用的模型服务或兼容的 OpenAI API 提供方，不要在仓库中保存 API Key。
2. 将环境变量 `POWERCONTEXT_SCOPE` 的 `REPLACE_WITH_SCOPE_ID` 改为验证脚本输出的实际 scope ID。
3. 将 `POWERCONTEXT_URL` 改为 Dify 能访问的 PowerContext 地址。若 Dify 在 Docker 中运行，通常使用 `http://host.docker.internal:8000`，不要把容器的 `127.0.0.1` 当作宿主机地址。
4. 如果 PowerContext 或接入网关要求 Bearer 鉴权，在 Dify 的 secret 类型环境变量 `POWERCONTEXT_TOKEN` 中配置令牌。导出应用时不勾选导出 secret 值。
5. 确认两个 `http-request` 节点分别为：
   - `POST /v1/memory/remember`：写入当前玩家输入。
   - `POST /v1/context/prepare`：以“玩家选择”标记和当前问题召回历史。统一标记用于在没有配置向量模型时，也能让“我之前去了哪里”这类不含原答案字面的问题找到玩家选择；当前问题仍参与相关度排序。
6. LLM 节点直接接收当前用户输入，并关闭内置会话记忆（`memory: null`），从 PowerContext 的召回结果回答历史选择问题。
7. 若你的 Dify 版本调整了节点字段，以 Dify 实际生成的 HTTP Request 节点为准重新绑定 URL、请求体和响应变量，然后重新导出 DSL。

## 验证样例

按顺序执行两轮对话：

1. 第一轮输入：`我选择前往森林并带上地图。`
2. 第二轮输入：`我之前去了哪里，带了什么？`

预期 NPC 明确回答“森林”和“地图”，而不是只给出通用冒险回复。保留两轮输入输出、HTTP 节点执行记录，以及 PowerContext 的记忆写入和召回日志作为技术结合证据。

页面验收使用新建的空 Scope，不能直接复用独立验证脚本已写入答案的 Scope。先确认召回状态为 `empty`，再执行两轮对话，并核对第一轮新建的 entry ID 出现在第二轮召回中。

2026-10-30 已在 Dify Cloud 完成上述验收，第二轮实际回答包含“你之前选择前往森林，并带上了地图”。详细结果见 `dify/evidence/README.md`。验收使用的临时 HTTPS 网关已关闭；重新运行 Cloud 应用时需要配置当前有效的安全公网地址和令牌。

`verify_powercontext.py` 可以直接复核服务端记忆闭环，但它不替代 Dify 的导入和节点运行验证。提交时应包含 Dify DSL、本说明、实际两轮运行结果和对应日志或截图。
