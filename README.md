# Content_Safety_Backend · 内容安全治理平台后端

内容安全治理平台业务后端，使用 FastAPI、SQLAlchemy 和 MySQL。业务后端默认端口为 `8000`；真实训练、模型对照评估等功能还会使用独立的 `content-security-llm` 模型服务（通常为 `8001`）。

后端各文件职责、目录分工与阅读顺序见 [后端文件作用与阅读指南](docs/backend-file-guide.md)。

本文按 2026-11-23 当前后端源码整理，包含本地工作区实现；接口字段与具体行为以对应版本代码和 `/openapi.json` 为准。本文中的密码、令牌、样本路径和资源 ID 示例均需替换为自己的实际值。

## 阅读导航

- 初次运行：阅读“本地启动”，完成依赖、数据库、环境变量和登录配置。
- 了解系统：阅读“业务范围与服务关系”“目录结构”和“配置变量”。
- 前后端联调：阅读“接口与认证约定”“智能体轨迹审计使用流程”。
- 开发和验证：阅读“测试、脚本与持续集成”“后台任务与运行目录”。
- 服务器运维：阅读“Linux 服务器后台启动与重启”“连接旧数据库时会发生什么”和“常见启动问题”。
- 数据库迁移：阅读“将 SQL 备份恢复到新的 MySQL 容器”；该节保留已有备份的专用恢复说明，不代表仓库分发数据库备份。

## 业务范围与服务关系

本项目负责平台业务 API、登录权限、资源与版本管理、任务记录、治理结果、合规审计以及智能体轨迹的导入、规则分析和导出。当前系统同时存在真实处理、模型服务代理和演示能力，不能把所有接口都理解成真实模型推理。

| 业务模块 | 后端提供的主要能力 | 主要代码 |
| --- | --- | --- |
| 登录与系统资源 | 登录、当前身份、角色、用户及通用资源查询管理 | `app/core/auth.py`、`app/routers/auth.py`、`app/routers/resources.py` |
| 数据资源 | 数据集、样本、版本快照、资源汇总、谱系关系 | `app/routers/data_resources.py`、`app/services/dataset_version_service.py`、`app/services/lineage_service.py` |
| 数据治理 | 文件/数据库接入、处理任务、异常/风险/价值结果读取、人工复核与派生版本 | `app/routers/data_governance.py`、`app/services/file_ingest.py`、`app/services/database_ingest.py`、`app/services/process_execution.py` |
| 模型工作台 | 模型和服务登记、调用记录、调用与任务关系同步 | `app/routers/model_workbench.py`、`app/routers/training_tasks.py` |
| 训练与模型对照 | 对接独立模型服务，读取真实进度、产物、模型版本与评估结果 | `app/routers/training_tasks.py`、`app/services/model_comparison.py` |
| 测试评估 | 指标与修订、评估任务、运行记录、样本结果、审计快照评估 | `app/routers/evaluations.py`、`app/services/audit_evaluation.py`、`app/services/risk_evaluation_service.py` |
| 全链路合规 | 合规上下文、谱系、告警、证据、审计、评论和复核 | `app/routers/compliance.py`、`app/services/compliance_service.py` |
| 智能体轨迹审计 | 批次上传解析、统计指标、思维路径、工具交互、规则发现、原文定位和 JSON/ZIP 导出 | `app/routers/agent_traces.py`、`app/services/agent_trace_*.py` |

服务关系如下：

```text
ContentSecurityVue（前端）
        │ HTTP /api/v1 + Bearer 登录令牌
        ▼
Content_Safety_Backend（本项目，通常 8000）
        ├── SQLAlchemy / PyMySQL ──► MySQL 平台数据库
        ├── 本机 storage/ ────────► 上传文件、认证密钥、导出产物
        └── HTTP + 模型服务令牌 ──► content-security-llm（通常 8001）
```

平台数据库支持直连或通过 Paramiko 建立 SSH 隧道。`content-security-llm` 是独立项目，不随本项目的 Uvicorn 启动。上传智能体轨迹的有限规则审计不需要调用大模型；训练、真实模型评估和部分治理业务需按具体功能配置模型服务。

## 技术栈和目录结构

运行依赖与锁定版本分别由 [pyproject.toml](pyproject.toml) 和 [uv.lock](uv.lock) 管理；项目没有 `requirements.txt`。

| 技术 | 用途 |
| --- | --- |
| Python ≥ 3.12、uv | 运行环境、虚拟环境和依赖锁定 |
| FastAPI、Pydantic、Uvicorn | HTTP API、参数校验、OpenAPI 和 ASGI 服务 |
| SQLAlchemy 2、PyMySQL | ORM、事务、MySQL 连接 |
| python-dotenv、Paramiko | 环境配置、可选 SSH 数据库隧道 |
| HTTPX、python-multipart、Pillow | 模型服务 HTTP 调用、上传、部分图像处理能力 |
| pytest、openpyxl（开发依赖） | 单元与集成测试、测试所需表格数据处理 |

```text
Content_Safety_Backend/
├── app/
│   ├── main.py             # 应用入口、路由、中间件和启动初始化
│   ├── core/               # 数据库、认证、响应、时间、日志及预置数据
│   ├── domain/             # Pydantic 请求/响应和能力定义
│   ├── models/             # ORM 表：平台、轨迹、复核与风险处置
│   ├── routers/            # HTTP 路由及参数入口
│   ├── services/           # 业务逻辑、任务、治理、规则、导出及后台同步
│   │   └── data/           # 随代码保存的经核验证据资源
│   ├── repositories/       # 数据访问和记录序列化
│   └── adapters/           # 能力适配器，含演示实现
├── scripts/                # 导入、迁移、对照和联调脚本
├── tests/                  # pytest 测试
├── docs/                   # 接口交接、规则说明及文件指南
├── .github/workflows/      # GitHub CI
├── .env.example            # 可公开的配置模板
├── pyproject.toml          # 依赖、Python 要求和 pytest 配置
├── uv.lock                 # 依赖锁文件
└── storage/                # 运行时生成，默认不提交 Git
```

数据库模型集中在 `app/models/`。轨迹模块以批次、文件资产/绑定、快照、投影、样本、事件、工具调用、关系、风险证据、统计、任务及产物关联数据；不能只复制某一张轨迹表来完整迁移批次。

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

## 配置变量

数据库模块会调用 `load_dotenv()`。推荐从后端根目录启动，以使相对的 `storage/` 路径一致。`.env.example` 只覆盖基础配置，下面的可选变量需按使用功能自行补充。请勿把真实 `.env`、密码、私钥和令牌写入仓库文档。

| 变量 | 代码默认值或状态 | 用途 |
| --- | --- | --- |
| `MYSQL_HOST` / `MYSQL_PORT` | `127.0.0.1` / `3306` | 平台数据库地址；自动 SSH 模式下从 SSH 主机视角解释。 |
| `MYSQL_USER` / `MYSQL_PASSWORD` | `root` / 空字符串 | 平台数据库账号；实际部署应配置专用账号。 |
| `MYSQL_DATABASE` | `content_safety` | 平台数据库名称；程序不会自动创建数据库本身。 |
| `SSH_HOST` / `SSH_PORT` / `SSH_USER` | 未启用 / `22` / 未设置 | 配置 SSH_HOST 后启用自动隧道。 |
| `SSH_PASSWORD` | 未设置 | SSH 密码认证。 |
| `SSH_PRIVATE_KEY_FILE` / `SSH_PRIVATE_KEY_PASSPHRASE` | 未设置 | SSH 私钥及可选口令。 |
| `SSH_HOST_KEY_FILE` | 未设置 | 额外的可信 OpenSSH 主机公钥文件；也读取系统可信主机记录。 |
| `SQL_ECHO` | `false` | 值为 true 时开启 SQLAlchemy SQL 输出；引擎配置隐藏 SQL 参数。 |
| `AUTH_SECRET` | 未设置 | 平台令牌签名密钥；缺省使用持久化文件 `storage/auth_secret.key`。更换密钥会使旧令牌失效。 |
| `LLM_TRAINING_BASE_URL` | `http://127.0.0.1:8001/api/v1/training` | 独立模型服务的训练 API 根地址。 |
| `LLM_EVALUATION_BASE_URL` | `http://127.0.0.1:8001/api/v1` | 模型对照评估等请求使用的 API 根地址。 |
| `LLM_GOVERNANCE_ORIGIN` | `http://127.0.0.1:8001` | 异常复核等治理结果代理使用的模型服务 origin。 |
| `LLM_ACCESS_TOKEN` | 空字符串 | 模型服务签发的访问令牌，与平台 Bearer 登录令牌不同。 |
| `LLM_SETUP_SECRET` | 空字符串 | 训练接入相关 setup 配置，具体调用见 `app/routers/training_tasks.py`。 |
| `AGENT_TRACE_LLM_DATABASE_URL` | 未设置 | 可选 SQLAlchemy 来源库连接串，用于模型服务轨迹来源授权与同步；不是平台 MYSQL_* 的替代配置。 |
| `AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC` | `false` | 开启历史价值分析来源同步/旧模拟轨迹兼容；默认隐藏相应历史模拟来源，不阻止实际上传解析。 |
| `AGENT_TRACE_ARTIFACT_DIR` | `storage/agent-trace-artifacts` | 轨迹导出文件保存目录。 |

当前 CORS 来源在 `app/main.py` 中固定列举 `localhost/127.0.0.1` 的 5173、5174 端口，没有对应的环境变量开关。前端经同源开发代理访问 `/api/v1` 时，应核对代理目标；若浏览器直接跨域访问其他前端端口，需要同步配置允许来源。

## 接口与认证约定

### 基础地址、响应及错误

业务接口前缀为 `/api/v1`，健康检查 `/health`、根路径 `/` 和 FastAPI 文档不使用此前缀。大部分接口采用以下响应封装：

```json
{
  "code": 0,
  "message": "请求成功",
  "data": {},
  "trace_id": "请求追踪标识",
  "timestamp": "2026-11-23T10:00:00"
}
```

请求可传 `X-Trace-Id`，响应返回对应 `X-Trace-Id`。业务时间使用 `Asia/Shanghai` 的时间辅助函数；各旧字段的时区标记形式不完全统一，不应将无时区后缀的值直接假设为 UTC。

调用方需要同时检查 HTTP 状态码和响应体 `code`。当前全局 ValueError 处理可能以 HTTP 200 返回业务 `code=400`；部分路由的 HTTPException/参数校验返回 FastAPI `detail`，轨迹路由则额外使用统一错误封装，不能假设所有错误体都完全相同。

### 登录和权限

除登录等明确豁免路由外，`/api/v1` 请求需要 `Authorization: Bearer <token>`。口令使用 PBKDF2-SHA256 散列；当前令牌是自定义 HMAC 签名串，不是标准 JWT，默认有效期为一年。

只读角色“审计员/auditor/只读”在中间件层禁止 POST/PATCH/PUT/DELETE；其他业务还会校验资源、操作者、原文权限或模型服务来源。原文查看与导出不能只凭前端按钮显隐授权。轨迹二进制下载入口使用短期签名下载票据，豁免普通 Bearer 校验不等于公开下载。

PowerShell 登录示例（后端已启动）：

```powershell
$api = 'http://127.0.0.1:8000/api/v1'
$loginBody = @{ username = 'admin'; password = '替换为当前账号密码' } | ConvertTo-Json
$login = Invoke-RestMethod -Method Post -Uri "$api/auth/login" -ContentType 'application/json' -Body $loginBody
$headers = @{ Authorization = "Bearer $($login.data.token)" }
Invoke-RestMethod -Uri "$api/auth/me" -Headers $headers
```

下面文档中的 HTTP 请求字段使用后端 `snake_case`。现有 Vue 客户端会将发送的 camelCase 字段转换为 snake_case，并将返回字段转换为 camelCase；其他客户端需要自行处理。

### 主要接口入口

下表用于导航，不替代完整的 OpenAPI；所有路径均相对于 `/api/v1`。

| 模块 | 代表接口 / 前缀 | 说明 |
| --- | --- | --- |
| 登录 | `POST /auth/login`、`GET /auth/me` | 登录、读取当前身份。 |
| 文件 | `POST /files` | multipart 的 file 字段上传，保存资产信息。 |
| 能力与任务 | `GET /capabilities`、`POST /tasks/execute`、`GET /tasks/{task_id}` | 能力目录、统一执行入口与任务状态；不同 capability_code 的 input 规则不同。 |
| 数据治理 | `/data-governance/*` | 接入、治理结果、异常和风险任务。 |
| 人工复核 | `/data-governance/anomaly-reviews/*`、`/data-governance/risk-dispositions/*` | 复核候选、变更集和处置流程。 |
| 测试评估 | `/evaluation/*`、`/metrics/*` | 指标修订、任务、运行、报告与审计快照。 |
| 模型训练 | `/training-tasks/*` | 训练任务管理与模型服务代理，详细路由见 Swagger。 |
| 全链路合规 | `/compliance/*`、`/alerts/*`、`/compliance-events/*` | 上下文、谱系、证据、审计、告警和训练期事件。 |
| 智能体轨迹 | `/compliance/agent-traces/*` | 轨迹接入、分析、定位与导出，见下节。 |
| 审计日志 | `GET /audit/logs`、`GET /audit/logs/{task_id}` | 操作和任务审计记录。 |

## 智能体轨迹审计使用流程

### 支持的输入与数据边界

轨迹接入支持 `.json/.jsonl`，单个轨迹资产上限为 **10 MiB**，绑定批次时会校验。通用 `/files` 上传器允许 TXT/CSV/JSON/JSONL/XLSX/PDF，最大 **2 GiB**；通用上传成功不代表该文件可作为轨迹解析。

| format_override | 输入结构 | 处理方式 |
| --- | --- | --- |
| `auto` | 由记录核心结构识别 | 自动选择以下受支持适配器。 |
| `openai_messages` | 含 messages 数组；工具请求为 tool_calls，返回用 tool_call_id 关联 | 按原消息顺序配对，保留参数、返回及 reasoning_content/reasoning 等原文。 |
| `langchain_trace` | 平台 runs 包装，含 id、parent_run_id、run_type、inputs/outputs 等 | 按根运行拆分记录，适配工具运行与返回；不是所有 LangSmith 原始格式都可直接导入。 |
| `agent_trajectory` | sample_id、events，可带 tool_events | 按外部智能体事件协议解析；示例见 ingest workspace 的 formats 和代码 EXAMPLE。 |

当前不支持 Anthropic tool_use/tool_result 格式；无法识别的结构不会静默变成正确轨迹。样本中引用的图片路径或“图像已省略”文字，也不等同后端已经读取或理解图片内容。没有记录的时间、Token、原文和会话状态按各接口缺失语义返回，不凭展示需要补造。

### 从上传到可查询快照

1. 登录并调用 `POST /compliance/agent-traces/batches` 创建批次，JSON 为 `{"name":"轨迹审计测试"}`。
2. 调用 `POST /files` 上传 JSON/JSONL，保存返回的 file_id。
3. 调用 `POST /compliance/agent-traces/batches/{batch_id}/files`，提交当前 expected_version 及 files 列表，例如 `{"expected_version":1,"files":[{"file_id":"file_实际ID","format_override":"auto"}]}`。
4. 通过统一任务接口提交解析；写请求携带独立的 `X-Request-Id` 幂等标识。expected_version 必须取最近一次接口返回的批次版本，不能一直使用 1。
5. 轮询 `GET /tasks/{task_id}`。成功后读取发布的 snapshot_id，再查询样本和详情。只有部分记录成功的文件可能标记 partial，应同时检查文件级错误与成功解析数。

解析任务请求示例：

```http
POST /api/v1/tasks/execute
Authorization: Bearer <平台登录令牌>
X-Request-Id: <本次操作唯一标识>
Content-Type: application/json
```

```json
{
  "capability_code": "agent_trace_ingest",
  "name": "解析上传轨迹",
  "input": {
    "batch_id": "batch_实际ID",
    "expected_version": 2,
    "mode": "parse_pending"
  },
  "config": {}
}
```

mode 支持 `parse_pending`、`retry_failed`、`reparse_all`；可选 file_ids 指定批次内文件。解析成功发布新快照，历史快照保留；不能把后台新解析结果和前端仍固定查询的旧 snapshot_id 混为同一份结果。

批次写操作使用版本校验和幂等记录：同一个 X-Request-Id 重试相同请求可取得原结果，改内容却复用请求 ID 会冲突；新操作请使用新 ID。删除使用 `POST /compliance/agent-traces/batches/{batch_id}/deletions`：删除文件传 file_id，删除空批次传 null；仅允许满足后端解析状态、成功数、空批次和运行任务限制的对象。

### 查询、定位和统计

| 接口 | 主要参数 | 返回用途 |
| --- | --- | --- |
| `GET /compliance/agent-traces/workspace` | view=ingest/tools/execution/statistics，batch_id、snapshot_id、include 等 | 批次/样本选项或对应工作区；execution 的 dashboard 可返回批次 tool_call_summary。 |
| `GET /compliance/agent-traces/statistics` | batch_id、snapshot_id、language 等 | 样本列表、Token、调用、行为及数据完整性统计。 |
| `GET /compliance/agent-traces/batches/{batch_id}/samples` | view、snapshot_id、projection_version、page/page_size、筛选条件 | 对应视图的样本摘要列表。 |
| `GET /compliance/agent-traces/batches/{batch_id}/samples/{trace_id}` | view=tools，section=overview/calls/selection/sequence | 工具统计、调用页、单次调用、时序分段。selection 需要 call_id。 |
| 同上详情接口 | view=execution，section=overview/events/selection/graph | 推理原文、事件页、选中事件和图分段；selection 需要 event_id。 |
| `GET /compliance/agent-traces/batches/{batch_id}/competition-report` | snapshot_id，可兼容 snapshotId | 在 data.jsonl 中返回逐样本与 aggregate 的提交文本；需要原文查看权限。 |

批次、样本、快照和 projection_version 共同确定查询上下文。签名图游标必须用于同一上下文和操作者；selection 返回目标事件所在日志页及 graph_locator，可据此请求 section=graph 补载节点。不能用展示标签 STEP-03 替代真实 event_id。

工具页字段 `metrics.success_count/error_count/not_executed_count/inefficient_count` 分别为当前样本全部调用的成功、失败、拦截和 C2 重复数，不受调用列表分页影响。`unknown_status_count` 单独保留未知状态。批次 tool_call_summary 则统计固定快照的完整调用请求，工具返回不重复计数。

原文按现有权限和脱敏模式返回；执行详情默认 redacted，授权 raw 模式才能查看未脱敏内容。`reasoning_original` 只采用真实来源日志，缺失时标记 not_provided，不能用生成摘要替代。长列表预览可能截断部分字段，完整证据应使用对应详情或受权导出，不能把预览当原始文件。

### 三类规则与结果含义

| 规则类别 | 当前实现 | 结果含义 |
| --- | --- | --- |
| 安全风险 | `agent_trace_rules.py`、`agent_trace_security.py`、`agent_trace_attack_rules.py` | 明确约束、权限、外部注入及六类攻击动作等有限规则；保留原始证据，内部全部发现与 B3 首次风险投影分开。 |
| 工具失败 C3 | `agent_trace_call_status.py` | 依据返回、元数据、退出码和诊断识别；排除正常探测否定结果及预期目标崩溃，外层成功不能掩盖明确内部失败。 |
| 不必要重复 C2 | `agent_trace_duplicates.py` | 参数相同只形成候选；要求上下文冗余证据或符合条件的静态文件重读，排除正常轮询、必要复核、状态变化与修复重试。 |

普通运行失败、重试和重复不会自动等同安全风险。规则面板区分审计完成有发现、审计完成零命中、等待审计和结果缺失。有限规则未命中不等于证明全面安全。

完整命中条件、排除分支和代码入口见 [三类规则汇总表](docs/agent-trace-rules-summary.md)、[风险规则详解](docs/agent-trace-risk-rules.md) 和 [C2 判定说明](docs/agent-trace-c2.md)。C2/C3 可按保存原文在读取时修正展示，不改写原始快照；安全规则版本升级则由后台针对支持的来源发布新快照。

### 导出与下载

轨迹异步导出使用 `POST /tasks/execute`，capability_code 为 `agent_trace_export`；input 包含固定快照、视图、范围和导出格式，具体组合由 `agent_trace_export.py`、`agent_trace_reports.py` 校验。

| 输出 | 获取方式 / 内容 |
| --- | --- |
| 当前视图 JSON、样本 JSON | 异步任务返回产物；不同 view 的导出范围与字段不同。 |
| 普通视图 ZIP | 可按支持项包含规范化 JSON、统计、CSV、证据索引、离线 HTML 等，具体以当前视图校验为准。 |
| execution 风险结果 ZIP | `view=execution, export_kind=package, format=zip`；当前专用包包含 submission.json、first-risks.json、all-risks.json，需要原文和导出权限。 |
| submission.jsonl 提交 | competition-report 接口的 data.jsonl，或本地导入脚本生成；与上述 ZIP 中数组形式的 submission.json 文件不同。 |

任务成功后，从 result.artifacts 取得 artifact_id，再调用 `POST /compliance/agent-traces/artifacts/{artifact_id}/download-tickets`（请求体 `{}`，带 X-Request-Id）取得短期 download_url；随后 GET 票据 URL 下载二进制。不要把票据 URL 长期保存为公开链接。PNG 导出不是当前受支持的导出格式。

## 后台任务与运行目录

轨迹同步器 `TraceSynchronizer` 在应用 lifespan 中启动，以进程内线程周期运行；默认约每 10 秒一轮，处理待解析/导出任务、统计物化、支持的规则版本升级和过期产物清理。任务接口也会为部分任务安排 FastAPI BackgroundTasks；并非所有能力都使用同一种后台执行方式。

当前项目没有统一的外部任务队列部署要求。开发时建议先运行单个 Uvicorn worker；增加多进程前需逐项确认启动初始化、线程循环及各业务任务的并发领取逻辑，不能假设所有任务都有分布式唯一消费保证。reload 会重启应用并重跑初始化。

| 目录 / 文件 | 内容和维护方式 |
| --- | --- |
| `storage/uploads/` | 通用上传原文件；数据库记录之外需要备份这些文件。 |
| `storage/agent-trace-imports/` | 本地赛题导入脚本复制的输入和生成的 submission.jsonl。 |
| `storage/agent-trace-artifacts/` 或配置目录 | 导出产物；有数据库到期时间和后台清理逻辑，不能保证永久保留。 |
| `storage/auth_secret.key` | 未设置 AUTH_SECRET 时的签名密钥；更换或丢失影响既有令牌及签名内容。 |
| `logs/` | 运维启动命令可生成的日志和 PID 文件。 |
| `db/backups/` | 本机数据库备份；默认被 Git 忽略，不随源码下载。 |

备份和迁移应同时考虑 MySQL、所需 storage 原文件、签名密钥和外部模型服务产物。复制 SQL 不会自动复制文件，复制 Git 仓库也不会自动带上被忽略的运行数据。

## 测试、脚本与持续集成

### 日常测试

```powershell
uv sync --locked
uv run pytest -q
```

项目 pytest 配置使用 tests/，并把项目根加入 Python 路径。许多集成测试使用隔离 SQLite 和 TestClient，可在不启动真实 MySQL/模型服务的情况下验证，但这不替代目标 MySQL 的迁移或真实模型联调。不要以直接启动生产应用的方式代替隔离测试，启动本身会写入配置的数据库。

仅检查轨迹三类规则时可运行：

```powershell
uv run pytest tests/test_agent_trace_rules.py tests/test_agent_trace_security.py tests/test_agent_trace_attack_rules.py tests/test_agent_trace_call_status.py tests/test_agent_trace_c2.py -q
```

外部五条赛题文件的集成校验需显式指定实际路径：

```powershell
$env:SCORING_SAMPLES_PATH = 'D:/测试数据/samples.jsonl'
$env:SCORING_REFERENCE_PATH = 'D:/测试数据/reference_answers.jsonl'
uv run pytest tests/test_agent_trace_competition.py tests/test_agent_trace_reviewed_evidence.py -q
```

这些路径是示例，仓库不保证包含外部数据。未配置外部文件的测试可能跳过；具体测试数随代码版本变化，不在 README 中固定承诺通过数量。

### 常用维护脚本

| 脚本 | 用途与执行影响 |
| --- | --- |
| `scripts/import_scoring_samples.py` | 通过持久化工作流导入本地赛题 JSONL，复制原文件、创建资产/批次、解析并生成提交；会写平台数据库和 storage，需要已有可用操作者。 |
| `scripts/compare_trace_submission.py` | 本地读取提交与参考文件，自动比较可结构化指标；不替代原文证据充分性和前端展示验收。 |
| `scripts/migrate_legacy_dataset_versions.py` | 专用旧版本表迁移；默认检查，`--apply` 写库，见后文迁移说明。 |
| `scripts/check_agent_trace_live.py` | 轨迹接口联调辅助，运行前检查脚本的目标服务、认证与写操作。 |
| `scripts/check_agent_trace_browser.cjs`、`check_agent_trace_pages_browser.cjs` | 浏览器联调辅助，需另行准备 Node/浏览器依赖与运行中的服务，不属于 Python 运行依赖自动安装内容。 |
| 其他 import/seed/setup/fix 脚本 | 多为特定数据的导入、演示或修复；先阅读源码与目标范围，不作为通用启动步骤批量执行。 |

导入与比较命令示例：

```powershell
uv run python scripts/import_scoring_samples.py 'D:/测试数据/samples.jsonl' --user-id 1 --name '赛题验证'
uv run python scripts/compare_trace_submission.py 'D:/测试数据/submission.jsonl' 'D:/测试数据/reference_answers.jsonl'
```

其中 user-id 必须替换成数据库中实际存在、状态有效且具有相应权限的用户，不能假设所有环境管理员 ID 都是 1。

GitHub CI 配置见 [.github/workflows/ci.yml](.github/workflows/ci.yml)：main 分支推送和 PR 触发，使用 uv 安装锁定依赖并运行 pytest。文档或代码只有在 git add、commit、push 后才会进入远端仓库；本地修改不会自动推送。

## 文档索引

| 文档 | 内容 |
| --- | --- |
| [后端文件指南](docs/backend-file-guide.md) | 文件职责、模块关系和源码阅读顺序。 |
| [三类规则汇总表](docs/agent-trace-rules-summary.md) | 风险、C3 失败、C2 重复的条件与排除逻辑。 |
| [风险规则详解](docs/agent-trace-risk-rules.md) | 12 类安全风险及证据合成。 |
| [C2 说明](docs/agent-trace-c2.md) | 不必要重复调用的上下文策略。 |
| [工具交互接入](docs/agent-trace-tools.md) | 工具交互页 API、数据和权限约定。 |
| [轨迹三页接入](docs/agent-trace-pages.md) | 接入、统计与执行视图的联调。 |
| [赛题说明](docs/scoring-samples.md) | 提交、公开样本测试与经核验证据资源。 |
| [前后端对齐记录](docs/frontend-backend-alignment-2026-11-20.md) | 特定日期的交接与兼容记录，不是永久最新接口规范。 |
| [模型对照](docs/model-comparison.md) | 真实模型对照评估配置及流程。 |
| [治理评估](docs/governance-evaluation.md) | 治理结果到评估的接入。 |
| [审计评估](docs/audit-evaluation.md) | 审计快照、指标和运行结果。 |
| [异常复核与风险处置](docs/governance-reviews.md) | 人工复核、审批、派生版本和模型服务协作。 |

## 将 SQL 备份恢复到新的 MySQL 容器

以下步骤将已有 SQL 导入一个独立的 MySQL 容器，后端继续在宿主机运行。SQL 文件是数据库备份，不需要构建成镜像；数据库数据通过 Docker 命名卷持久化。

本次备份为 `db/backups/content_safety_20261112_150707.sql`，约 406 MiB，源版本为 MySQL `8.0.46`，包含 `content_safety` 库的 74 张表。备份包含 `CREATE DATABASE`、`USE content_safety` 和 `DROP TABLE IF EXISTS`，**只能在确认目标的情况下导入；导入已有同名库会替换其中备份涉及的表。** 下面使用新的容器、数据卷和 `3308` 端口，与当前占用 `3307` 的 `content-safety-dev-mysql-1` 分开。

### 1. 准备 Docker 和备份文件

启动 Docker Desktop（Linux 使用 Docker Engine），确认已安装 Compose 插件。在后端根目录执行：

```powershell
docker version
docker compose version
Get-FileHash ./db/backups/content_safety_20261112_150707.sql -Algorithm SHA256
New-Item -ItemType Directory -Force ./db/backups/mysql-restore
```

本次备份 SHA-256 应为：

```text
bcd90dce3bb834d80e58b12fc63ae0097d32a55f60d71a0cd61e2d4e4c0b2b66
```

迁移到另一台机器时复制 SQL 文件，并保持后续命令中的路径正确。Linux 可用 `sha256sum` 校验、`mkdir -p ./db/backups/mysql-restore` 创建目录。备份文件和以下部署配置位于已被 Git 忽略的 `db/backups/` 下。

### 2. 创建 Compose 配置和容器初始化密码

在 `db/backups/mysql-restore/compose.yml` 新建以下内容：

```yaml
services:
  mysql:
    image: mysql:8.0.46
    restart: unless-stopped
    environment:
      MYSQL_ROOT_PASSWORD: ${RESTORE_ROOT_PASSWORD:?请设置root密码}
      MYSQL_DATABASE: content_safety
      MYSQL_USER: content_user
      MYSQL_PASSWORD: ${RESTORE_APP_PASSWORD:?请设置业务账号密码}
    ports:
      - "127.0.0.1:3308:3306"
    volumes:
      - mysql_data:/var/lib/mysql
    command:
      - --character-set-server=utf8mb4
      - --collation-server=utf8mb4_0900_ai_ci
      - --max-allowed-packet=256M

volumes:
  mysql_data:
```

在同一目录新建 `.env`，两个密码分别替换为自己的值。使用单引号可避免密码中的 `$` 被 Compose 插值：

```dotenv
RESTORE_ROOT_PASSWORD='替换为新的root密码'
RESTORE_APP_PASSWORD='替换为新的content_user密码'
```

这是 **Compose 初始化配置**，与后端根目录的 `.env` 是两个文件。原 SQL 只备份了业务库，不包含 MySQL 系统账号；新容器的账号密码以此处初始化配置为准，平台 `system_users` 表中的登录账号及密码哈希则随 SQL 恢复。

### 3. 启动新容器并等待 MySQL 就绪

仍在后端根目录执行，固定项目名 `content-safety-restore`：

```powershell
docker compose --env-file ./db/backups/mysql-restore/.env -f ./db/backups/mysql-restore/compose.yml -p content-safety-restore up -d
docker compose --env-file ./db/backups/mysql-restore/.env -f ./db/backups/mysql-restore/compose.yml -p content-safety-restore logs --tail 80 mysql
```

等待日志出现最终的 `ready for connections`，并确认服务已监听 `3306`。如果 `3308` 被其他进程占用，修改 Compose 的宿主机端口，并同步修改后端的 `MYSQL_PORT`。

### 4. 复制 SQL 并手动导入

先把备份复制到新容器：

```powershell
docker compose --env-file ./db/backups/mysql-restore/.env -f ./db/backups/mysql-restore/compose.yml -p content-safety-restore cp ./db/backups/content_safety_20261112_150707.sql mysql:/tmp/content_safety.sql
docker compose --env-file ./db/backups/mysql-restore/.env -f ./db/backups/mysql-restore/compose.yml -p content-safety-restore exec mysql mysql --default-character-set=utf8mb4 --max-allowed-packet=256M -u root -p
```

提示 `Enter password:` 时输入刚设置的 `RESTORE_ROOT_PASSWORD`。进入 `mysql>` 后执行（不要输入提示符本身）：

```sql
SOURCE /tmp/content_safety.sql;
SELECT COUNT(*) AS table_count
FROM information_schema.tables
WHERE table_schema = 'content_safety' AND table_type = 'BASE TABLE';
USE content_safety;
SELECT COUNT(*) AS dataset_count FROM datasets;
SELECT COUNT(*) AS model_count FROM models;
SELECT COUNT(*) AS user_count FROM system_users;
EXIT;
```

等待整份文件导入完毕，检查过程中是否有 `ERROR`，不要仅凭最后一条查询成功判断导入完整。此次备份恢复后应有 74 张基础表；表数量仅是基础检查，还应核对关键记录和页面。密码交互输入，不要把真实密码写进 README 或命令参数。采用容器内 `SOURCE`，避免 PowerShell 文本管道处理大型 SQL 时改变编码。

命名卷会保留导入的数据；普通重启和 `docker compose ... down` 不会删除该卷。不要使用 `down -v`，除非明确要删除该恢复实例的数据。初始化环境变量只对空数据卷生效；修改 Compose `.env` 不会自动修改已有账号密码，应先在 MySQL 中执行相应 `ALTER USER`，再更新连接配置。

### 5. 修改后端数据库连接

编辑 **后端根目录** 的 `.env`（不是上面的 Compose `.env`）：

```dotenv
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3308
MYSQL_USER=content_user
MYSQL_PASSWORD=填写与RESTORE_APP_PASSWORD相同的实际密码
MYSQL_DATABASE=content_safety
```

切换前停止正在运行的后端，再按以下顺序操作：

1. 从根目录 `.env` 移除或注释 `SSH_HOST`，本机容器直连不需要 SSH；如果系统环境也设置了该变量，需要同步移除。
2. 检查启动终端或服务管理器是否还设置了旧 `MYSQL_*` 系统环境变量，它们会优先于 `.env`。仅修改文件而不重启进程不会切换现有连接池。
3. 可先用容器内客户端验证业务账号密码和授权：执行下面的登录命令，输入 `RESTORE_APP_PASSWORD`，然后执行 `SELECT DATABASE(); SHOW TABLES; EXIT;`。
4. 确认恢复完成后再启动后端；后端启动会继续执行前文说明的迁移、演示数据和回填逻辑，可能改变已恢复的数据。

```powershell
docker compose --env-file ./db/backups/mysql-restore/.env -f ./db/backups/mysql-restore/compose.yml -p content-safety-restore exec mysql mysql -h 127.0.0.1 -u content_user -p content_safety
```

退出 MySQL 客户端后重启后端并检查健康接口和业务数据：

```powershell
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

在另一终端执行：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
```

### 6. 不同部署位置的连接地址

| 后端所在位置 | MYSQL_HOST | MYSQL_PORT | 说明 |
| --- | --- | --- | --- |
| 与 Docker 同一台宿主机 | `127.0.0.1` | `3308` | 使用宿主机映射端口，本节默认方案。 |
| 与 MySQL 同一 Compose 网络的容器 | `mysql` | `3306` | 使用服务名和容器内端口；后端容器中的 `127.0.0.1` 指向后端自己。单独启动的容器须先加入同一网络。 |
| 本机后端通过 SSH 连接远程 Docker 宿主机 | `127.0.0.1` | `3308` | 配置远程宿主机的 `SSH_HOST` 等参数；MYSQL 地址从远程 SSH 主机视角填写。 |

默认端口只绑定宿主机回环地址，其他机器不能直接访问。远程部署建议通过本 README 的 SSH 隧道方式访问；若采用远程直连，需另外配置端口绑定、网络访问规则和匹配的数据库账号授权。

若模型服务也要读取恢复后的共享数据库，需要按该服务实际配置同步切换；不能只切换业务后端而让模型服务继续读取旧库。SQL 不包含宿主机上的 `storage/`、模型文件或训练产物，这些文件需要另外迁移。

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

### 旧版 dataset_versions 缺少 version_id

如果启动报 `Unknown column 'dataset_versions.version_id'`，且旧表使用
`id/dataset_id/version/source_version/task_id/samples/created_at`，其中 `samples`
是 JSON 对象列表，可使用专用迁移脚本。执行前停止所有写入该库的服务和任务；
此脚本不通过启动应用执行，不会顺带运行其他初始化逻辑。

```bash
cd /root/yjp/Content_Safety_Backend
uv run python scripts/migrate_legacy_dataset_versions.py
uv run python scripts/migrate_legacy_dataset_versions.py --apply
```

第一条仅检查，确认目标库和版本/样本数量正确后再执行第二条。
脚本会为 `dataset_versions`、`dataset_version_items`、`dataset_records` 创建带时间戳的库内备份表，
保留旧版本列，将旧快照原文追加到样本表，并登记新版版本清单、清单哈希及父版本引用。
不会将 `record-1` 直接猜测为数据库记录 1，也不会覆盖已有样本。
因此迁移后 `dataset_records` 记录数会增加，版本条数和各版本快照内容应保持一致。
此备份在同一个数据库内，不替代独立的完整数据库备份。

MySQL DDL 不能整体事务回滚；数据转换和校验使用独立事务，失败时不要删表，
保留脚本输出的备份表名和错误信息进行排查。脚本遇到已填充的新版版本 ID 会停止重复迁移。
此方案保留旧自增主键，通过唯一索引保证新版 `version_id` 唯一，属于兼容性迁移。
隔离测试验证了快照和版本关系转换，未代替目标服务器上的 MySQL 恢复演练。

迁移成功后，按前述启动命令重启后端，并检查 `/health` 以及历史版本内容。

- `Unknown database`：目标数据库尚未创建，先建库；程序只自动建表。
- `Access denied`：核对账号、密码、账号 Host 和授权，启动需要 DDL 与写入权限。
- `Unknown column` 或迁移报错：旧表结构可能超出兼容范围，按日志核对具体表结构，不能仅靠 `create_all()` 修复。
- SSH 主机公钥错误：检查可信公钥文件、SSH 地址和端口；远端开发机重启后端口可能变化。
- 空库首次登录失败：按上述说明重启一次，为新建预置账号补上密码；已有账号检查密码和 `status`。
- 端口 `8000` 被占用：先确认是否已有后端进程；更换端口时同步调整前端代理或反向代理。

### 异常复核与风险处置

支持对模型服务已保存的异常、风险结果进行人工复核，并在审批后发布派生数据版本。
新增接口、模型服务配置、新表、CSV 参考标签规则及赛题结果比较工具见
[异常复核与风险处置接入说明](docs/governance-reviews.md)。
