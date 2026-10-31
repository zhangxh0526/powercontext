# MiniMax SDD 工程上下文管理

这个最小实现按开发任务检索当前有效的规格、设计、代码和测试来源，并把生成的上下文保存到 seekdb。规格重新导入后使用新修订；任务上下文在模块重启后仍可恢复并用于下一次任务。

## 组成

- `minimax/context_manager.py`：文档校验、参数化 SQL 检索、规格更新和任务记录持久化。
- `samples/`：规格 v1/v2、设计、代码来源和测试来源。
- `src/status.py`：已实现的 `normalize_status`。
- `samples/broken_status.py`：修改前的未实现版本，用于对照验证。
- `tests/`：目标行为测试和真实 seekdb 集成测试。

数据库使用 `sdd_documents` 和 `sdd_task_records` 两张表。每次查询都由 seekdb 执行，返回的 `doc_id`、修订号与 `source_path` 一并写入任务记录。不是把 JSON 文件当作数据库替代品。

## 运行环境

- Python 3.11+
- PyMySQL 1.1+
- 可连接的 seekdb/OceanBase MySQL 协议实例

安装依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

配置连接信息。示例默认值仅用于本地测试，不要在仓库中写入真实密码：

```bash
export SEEKDB_HOST=127.0.0.1
export SEEKDB_PORT=12881
export SEEKDB_USER=root
export SEEKDB_PASSWORD='replace-with-your-own-password'
export SEEKDB_DATABASE=minimax_sdd
export PYTHONPATH=.
```

程序使用独立的 `minimax_sdd` 数据库。`demo.py` 和集成测试会重建其中的两张演示表，请勿将 `SEEKDB_DATABASE` 指向业务数据库。

## 验证

### 1. 目标代码修改前失败、修改后通过

```bash
SDD_STATUS_MODULE=samples.broken_status python3 -m unittest tests.test_status
SDD_STATUS_MODULE=src.status python3 -m unittest tests.test_status
```

第一条命令应因 `NotImplementedError` 失败；第二条命令的全部目标行为测试应通过。`verify.sh` 自动检查这两个结果，不会在修改前失败时误判为成功。

### 2. 真实数据库、规格更新和重启复用

```bash
python3 -m unittest tests.test_context_manager
```

验证过程：

1. 从空表导入规格 v1、设计、代码和测试来源。
2. 按任务检索四类来源，确认规格修订为 1，并保存生成的上下文。
3. 关闭并重新创建上下文模块，读取同一任务记录并用于后续任务。
4. 重新导入规格 v2，确认后续检索使用修订 2 和“空字符串”的新规则。
5. 测试结束后重建演示表。

### 3. 一键验证

```bash
PYTHON=python3 bash verify.sh
```

`verify.sh` 依次检查修改前失败、修改后通过和数据库集成测试。

### 4. 查看实际上下文

```bash
python3 demo.py
```

输出首次检索、重启恢复和规格更新后的 JSON，包含每个来源的 `doc_id`、类型、修订号和路径。

## 实现与代码修改记录

本次修改增加了上下文存储与检索实现，以及可复现的目标实现和验证样例。目标修复位于 `src/status.py`：统一大小写、空格和连字符，限制合法状态并拒绝未知值或空字符串。`samples/broken_status.py` 保留修改前的未实现版本，便于复核同一组测试的失败与通过。代码检索结果用于生成任务上下文，而不是仅作展示；任务上下文写回 seekdb 后可跨重启恢复。
