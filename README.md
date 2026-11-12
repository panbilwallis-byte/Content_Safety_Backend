# Content_Safety_Backend

内容安全治理平台业务后端，使用 FastAPI、SQLAlchemy 和 MySQL。业务后端默认端口为 `8000`；真实训练、模型对照评估等功能还会使用独立的 `content-security-llm` 模型服务（通常为 `8001`）。

## 本地启动

### 1. 准备环境和依赖

- Python 3.12 或更高版本（项目 `.python-version` 指定 3.12）。
- 安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)。
- 准备可连接的 MySQL，推荐 MySQL 8.0，并创建数据库和专用账号。

在后端项目根目录执行，后续命令也都在此目录运行：

```powershell
cd "D:\资料\01项目-内容治理\Content_Safety_Backend"
uv sync --locked
```

`uv sync` 会安装依赖并创建 `.venv`；使用 `uv run` 不需要手动激活虚拟环境。

### 2. 准备数据库

**程序会自动创建表，但不会创建 MySQL 数据库本身。** 新环境可由数据库管理员执行以下 SQL；已有数据库和账号时确认授权即可：

```sql
CREATE DATABASE IF NOT EXISTS content_safety
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'content_user'@'localhost'
  IDENTIFIED BY '替换为实际数据库密码';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES
  ON content_safety.* TO 'content_user'@'localhost';
```

上例适用于 MySQL 将连接识别为 `localhost` 的情况；远程直连由管理员按实际客户端来源配置账号 Host 和授权。只读账号不能完成启动时的建表、补列和数据回填。

连接旧库前请先阅读下方“连接旧数据库时会发生什么”，备份并优先在副本上验证。

### 3. 配置 `.env`

首次配置且根目录尚无 `.env` 时复制模板；已有文件直接修改，不要覆盖：

```powershell
Copy-Item .env.example .env
```

Linux 对应命令为 `cp .env.example .env`。编辑配置：

```dotenv
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_USER=content_user
MYSQL_PASSWORD=替换为实际数据库密码
MYSQL_DATABASE=content_safety
```

`MYSQL_DATABASE` 决定初始化写入哪个库。本地手动转发 MySQL 时，将端口改为实际转发端口，例如 `3307`。代码通过 `load_dotenv()` 读取配置，已有系统环境变量优先于 `.env`；修改配置后需要重启进程。

如果需要由后端自动建立 SSH 隧道，额外配置：

```dotenv
SSH_HOST=填写服务器地址
SSH_PORT=22
SSH_USER=填写SSH用户
SSH_PASSWORD=填写SSH密码
SSH_HOST_KEY_FILE=%USERPROFILE%/.ssh/server_ed25519.pub
```

- `SSH_HOST_KEY_FILE` 是从已信任服务器取得的 OpenSSH 主机公钥文件，例如服务器的 `/etc/ssh/ssh_host_ed25519_key.pub`；代码也加载系统已信任主机记录，并拒绝未知主机。
- 私钥登录时改用 `SSH_PRIVATE_KEY_FILE`，删除 `SSH_PASSWORD`；私钥有口令时配置 `SSH_PRIVATE_KEY_PASSPHRASE`。
- 启用 SSH 后，`MYSQL_HOST` 和 `MYSQL_PORT` 是 **SSH 服务器视角** 的 MySQL 地址，常见为 `127.0.0.1:3306`，不是本机手动转发的端口。
- 不使用自动 SSH 隧道时，不要配置 `SSH_HOST`。

### 4. 启动并检查

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

开发时可加 `--reload`，代码变化会重启并再次执行初始化。需要其他机器直接访问时，将监听地址改为 `0.0.0.0`，并配置相应网络访问规则。

启动会先检查数据库连接，再建表、迁移和补充数据。看到 `MySQL 数据表初始化完成` 和 Uvicorn 的 `Application startup complete` 后访问：

- 健康检查：<http://127.0.0.1:8000/health>，响应中应有 `data.status = "ok"`。
- Swagger 接口文档：<http://127.0.0.1:8000/docs>。
- OpenAPI：<http://127.0.0.1:8000/openapi.json>。

PowerShell 可执行 `Invoke-RestMethod http://127.0.0.1:8000/health`。按 `Ctrl+C` 停止服务。

### 5. 登录和模型服务联调

预置管理员为 `admin / admin123`，只读演示账号为 `auditor / auditor123`。

**当前启动顺序先执行 `ensure_user_passwords()`，再由 `seed_initial_data()` 创建账号。空库或缺少预置账号的旧库首次启动后，需要停止并再次启动，才能为新建账号补上初始密码。** 已有非空密码不会被这一步重置。公开部署前应更换代码中的演示账号口令。

登录接口为 `POST /api/v1/auth/login`，JSON 请求示例：

```json
{"username": "admin", "password": "admin123"}
```

取得响应的 `data.token` 后，调用其他 `/api/v1` 接口时携带 `Authorization: Bearer <token>`。

如需真实训练和模型对照评估，另行启动 `content-security-llm`，并在业务后端 `.env` 配置实际地址与模型服务签发的访问令牌：

```dotenv
LLM_TRAINING_BASE_URL=http://127.0.0.1:8001/api/v1/training
LLM_EVALUATION_BASE_URL=http://127.0.0.1:8001/api/v1
LLM_ACCESS_TOKEN=填写模型服务签发的有效令牌
```

平台登录令牌与 `LLM_ACCESS_TOKEN` 用途不同。模型服务未运行或授权配置不正确时，相关真实调用功能不可用。智能体轨迹来源、独立来源库等配置见 [工具交互接入说明](docs/agent-trace-tools.md) 和 [三页接入说明](docs/agent-trace-pages.md)。

## Linux 服务器后台启动与重启

首次部署仍需先完成依赖安装、建库和 `.env` 配置。在后端根目录执行：

```bash
uv sync --locked
mkdir -p ./logs
nohup uv run uvicorn app.main:app \
  --host 127.0.0.1 \
  --port 8000 \
  >> ./logs/backend.log 2>&1 &
echo $! > ./logs/backend.pid
```

重启前确认 PID 对应本项目进程，然后停止服务：

```bash
ps -fp "$(cat ./logs/backend.pid)"
kill "$(cat ./logs/backend.pid)"
```

等旧进程退出后，重新执行上面的 `nohup` 和 PID 保存命令。查看日志与健康状态：

```bash
tail -n 100 ./logs/backend.log
curl http://127.0.0.1:8000/health
```

`127.0.0.1` 监听适合通过同机反向代理提供服务。部署时保留 `storage/`（上传文件、认证密钥和导出产物等）；未配置 `AUTH_SECRET` 时，认证密钥保存在 `storage/auth_secret.key`。

## 连接旧数据库时会发生什么

**有初始化代码。把 `.env` 指向旧库后，只要启动成功，程序会直接在该库创建缺失表、执行已编写的字段迁移，并写入符合条件的预置数据，不需要另跑初始化脚本。初始化不只是新增，也包含更新已有记录。**

启动入口是 [`app/main.py`](app/main.py) 的 `lifespan()`：

| 阶段 | 代码位置 | 对旧库的影响 |
| --- | --- | --- |
| 检查连接、创建缺失表 | `app/core/database.py`、`Base.metadata.create_all()` | 数据库须已存在；按 ORM 创建缺失表，不会因此自动补齐已有表的全部字段。 |
| 补列、调整列、迁移 | `app/core/seed.py` 中的 `ensure_*_columns()`、`ensure_dataset_source_type_column()`、`ensure_user_passwords()` | 对任务、数据集、记录、模型、训练、评估、用户等部分旧表执行显式 DDL 和回填。 |
| 旧资源迁移 | `migrate_legacy_resources()` | 从旧 `resources` 表按名称补登到独立的 `datasets`、`models` 表。 |
| 基础数据和演示数据 | `seed_initial_data()` 及其他 `ensure_*` 函数 | 按各函数条件补充预置数据集、模型、用户、角色、指标、接入/处理/评估演示任务等。 |
| 历史关系回填 | 版本、谱系、合规、训练产物和模型调用相关启动函数 | 按已有数据补登版本快照、谱系边、合规记录、模型版本和调用任务索引。 |
| 智能体轨迹同步 | `app/services/agent_trace_sync.py` | 启动后台同步器，将符合来源和授权条件的历史任务结果导入轨迹表。 |

### 能否补齐所有缺失数据？

**不能保证全部补齐。** 当前是手写增量兼容逻辑，没有覆盖任意历史版本的完整数据库迁移系统：

- 大部分预置记录按名称、账号、编码或固定 ID 判断是否存在，但部分函数只在表为空时写入。例如 `ensure_demo_evaluations()` 在已有任意评估记录时就跳过，不会逐条补齐演示结果。
- `create_all()` 只创建缺失表，不会修复已有表的所有列、索引和约束。只有明确写出的迁移会执行；缺少未覆盖的字段、存在冲突数据或权限不足时，仍可能启动失败。
- 预置数据集名称、条数、容量等元数据不代表已经导入对应数量的真实样本；真实样本、模型权重、训练产物和外部模型服务的数据仍需实际导入或生成。
- 初始化分多个事务执行，并非一次整体事务；中途失败时，前面已提交的迁移和写入可能已经生效。

### 会覆盖旧数据吗？

当前启动流程没有清空整个数据库或删库重建的步骤，但 **不能承诺保留所有已有字段值**。例如：

- `ensure_model_columns()` 每次启动会按模型排序重新设置 `models.source`，并补登模型版本和默认服务。
- `ensure_process_demo_tasks()` 会对固定 ID `tsk_process_demo_06`、`tsk_process_demo_07` 的已有任务重新写入演示字段。
- `ensure_resource_display_values()` 会补充或规范化数据集容量、条数、质量分数，以及部分任务来源、容量和统计值；某些默认模型版本也会调整。
- 评估展示规范化会调整部分名称和测试编号，成功任务的零进度会回填为 100。

因此连接旧库前应备份，并在副本上检查。当前没有统一的“禁止演示数据初始化”或“只读启动”开关；要保留旧库原样，需要先调整启动初始化逻辑。

### 如果旧库只是外部业务数据源

`.env` 的 `MYSQL_*` 指定的是 **平台自身数据库**，上述建表和写入发生在这里。如果只是读取已有业务库内容，建议平台使用独立数据库，再通过“数据库接入”功能配置旧库作为来源。

[`app/services/database_ingest.py`](app/services/database_ingest.py) 的来源读取逻辑只接受受限制的 `SELECT` 查询，读取后将样本写入平台数据库，不会向来源库执行初始化建表或预置数据写入。若平台配置与来源配置指向同一个库，平台启动写入仍会发生在该库。

## 常见启动问题

- `Unknown database`：目标数据库尚未创建，先建库；程序只自动建表。
- `Access denied`：核对账号、密码、账号 Host 和授权，启动需要 DDL 与写入权限。
- `Unknown column` 或迁移报错：旧表结构可能超出兼容范围，按日志核对具体表结构，不能仅靠 `create_all()` 修复。
- SSH 主机公钥错误：检查可信公钥文件、SSH 地址和端口；远端开发机重启后端口可能变化。
- 空库首次登录失败：按上述说明重启一次，为新建预置账号补上密码；已有账号检查密码和 `status`。
- 端口 `8000` 被占用：先确认是否已有后端进程；更换端口时同步调整前端代理或反向代理。
