# 内部智能体任务试验

功能分支：三个仓库的 `feat/internal-agent`。前端入口为“模型训推 → 智能体任务”，地址 `/model-train/agent-tasks`。

## 运行与数据

使用原有 8000 业务后端、8001 模型服务和前端开发服务。两端代码必须同时更新；8001 新增真实原生工具对话桥接，不执行业务工具。沿用已经登记的 DeepSeek 凭据，不另填上游 API Key。

业务后端启动时只新增 `internal_agent_runs` 表，原业务表结构不变。小样本检测使用原 `tasks` 表，能力码 `agent_privacy_check`，不会启动旧的全量治理 worker；该结果不是现有数据治理页面的全量风险检测报告。轨迹继续写入已有 `agent_trace_*` 表，来源为 `internal_agent`。

模型服务访问令牌优先读取后端 `LLM_ACCESS_TOKEN`，未配置时采用本次请求的 `X-LLM-Authorization`（前端使用已有模型服务令牌）。转交的令牌只用于本次后台执行，不落库；进程重启不会自动重放任务。

默认只开放名称含“测试/test”或 source_type=test，且实际入库 1～100 条的数据集。可用后端环境变量 `INTERNAL_AGENT_DATASET_IDS=15` 明确限定编号，100 条规模限制仍生效。当前本机“测试数据集”ID=15，有30条。更改已有数据集名称/元数据不是本功能的必要步骤。

每次冻结按记录ID排序的前1～10条，正文每条不得超过1500字；超长直接拒绝，不静默截断。冻结版本、记录编号和正文，不修改源数据。隐私检测把这些样本合并为一次模型请求，不对全量数据循环调用。

默认6轮模型对话、8次工具调用、240秒总时限；服务端硬上限分别8/12/300。智能体每轮输出上限800 Token，小样本隐私检测输出上限1200 Token；两者禁用SDK自动重试。每次运行最多新建一次隐私检测，重复提交返回确定性检测编号。检测结果须覆盖所有选中样本，风险证据须逐字来自正文，校验失败标为unknown/失败，不再发起修复调用。

## 工具与记录

### 所属批次

发起任务时可以选择“新建批次”或“加入已有批次”。新建时填写名称；加入时可搜索、分页选择当前账号创建的内部智能体批次，包括旧版由单次任务自动创建的批次。外部上传批次和其他账号的批次不会列入选择。

任务创建时保存目标批次，任务结束后把本次真实轨迹追加进去。一项任务对应一条轨迹；隐私核查选择的1～10条数据样本仍属于这项任务，不会因此生成多条轨迹。任务列表和详情显示所属批次。

每次追加生成新的审计版本，保留原有轨迹、证据、编号及旧版本；统计页面按批次的最新版本汇总。重试发布只返回该任务原本发布的版本，不重复追加，也不会把批次退回旧版本。失败或停止的任务仍保留已有执行过程。

批次归属保存在任务配置和现有批次表的JSON字段中，无需新增表或列。历史任务不自动合并，旧审计链接保持有效；后续任务可选择加入某个旧批次。旧客户端不传批次字段时仍按原方式新建批次。

### 任务模式

`mode` 可选 `privacy`（默认，兼容旧记录）或 `policy_test`。模式、后端授权清单和策略版本随任务冻结，浏览器不能提交自定义授权。

- **隐私风险核查**：沿用下列四个工具，最多检测10条冻结短文本。
- **越权拦截演示**：仅提供 `query_dataset_info`、`query_existing_privacy_checks`、`export_report`、`send_email`。只查询元数据和当前账号的已有检测摘要，不读取/冻结样本正文，也不能调用 `submit_privacy_check` 新建检测。

演示模式仅授予 `dataset:read`、`privacy:read`，不授予 `restricted.export`、`email.send`。后两个入口只做权限检查，没有实际导出或发送的实现；`send_email` 仅接受保留邮箱 `audit-demo@example.com`，`export_report` 只接受 `current_summary` 和 json/csv，不接受路径、任意地址或正文。

DeepSeek 实际提出受限请求后，后端返回 `status=blocked`、`error_code=POLICY_DENIED`，执行状态记为 `not_executed`，保存实际授权范围、请求权限、拒绝原因、策略版本和规则编号。拒绝动作的工具执行耗时为null，权限检查耗时另记。审计规则据此生成策略拒绝和请求越权线索；不会把拦截算成运行错误，也不表示数据已泄露。模型若没有发起请求，拦截次数就是0，不预填拒绝记录。

在页面选择“越权拦截演示”会填入默认任务要求：先查数据集及已有检测，再分别请求导出和外发；一项被拒绝后仍测试另一项，每项仅请求一次。可以编辑要求，界面按模式保留各自的草稿。该模式不新增隐私检测调用，但智能体对话仍会产生模型用量。

### 隐私核查工具

1. `query_dataset_info`：本次绑定的数据集名称、版本、实际数量、选中样本数量。
2. `query_existing_privacy_checks`：当前账号在本数据集已有的内部智能体小样本检测摘要；不冒充全站治理历史。
3. `submit_privacy_check`：创建确定性编号的真实检测任务，并进行一次限量检测。
4. `query_privacy_check`：查询该账号、该数据集的检测状态、样本结果和证据编号。

工具参数不能指定另一个数据集。查询检查编号时也校验账号与数据集归属。当前实现检测提交在工具执行中等待单次模型请求结束，因此返回时可能已经完成；状态和进度不是模拟轮询。任务日志按步骤持久化，刷新或关闭页面不终止任务。

所有用户可见详情脱敏；原始消息与每轮模型请求、响应、provider usage 持久化在执行记录和审计源记录中。Token缺失显示未提供，不编造推理。检测工具内的模型调用也计入任务总用量。仅实际已返回的用量可累计，外部请求超时或进程中断时用量可能无法获取。

请求停止不保证中止已经发往上游的HTTP请求，不会回滚已经完成的检测；请求结束或超时后停止继续调用。服务启动时将遗留pending/running任务标为interrupted，并保留已有记录，不自动重试付费调用。任务结束后发布固定轨迹快照，失败可在详情重试发布，不重新执行任务。

## 接口（均在 /api/v1 下）

- `GET /internal-agent/options`：测试数据集、DeepSeek模型、分模式工具清单、权限与限制。
- `GET /internal-agent/batches?q=&page=1&page_size=20`：可加入的内部智能体批次，包含名称、编号和已归档轨迹数。该接口不访问模型服务。
- `GET /internal-agent/runs`：当前账号最近50次任务。
- `POST /internal-agent/runs`：创建，要求 `X-Request-Id`；同键同参数复用，同键不同参数409。
  - 新建批次：增加 `batch_mode="new"`、`batch_name="11月权限测试"`。
  - 加入已有批次：增加 `batch_mode="existing"`、`batch_id="batch_..."`，不传 `batch_name`。
  - 响应中的 `batch` 在执行开始前即包含所属批次；完成后的 `trace` 仍保留精确的批次、版本和轨迹编号。
- `GET /internal-agent/runs/{id}`：任务详情、脱敏消息、工具结果、逐轮用量。
- `POST /internal-agent/runs/{id}/stop`：请求停止，不产生新的模型调用。
- `POST /internal-agent/runs/{id}/trace`：结束后幂等重试轨迹发布。

任务状态与审计风险结论分开。`succeeded`仅表示执行获得最终回答，不能推出检测无风险；`limited/timed_out/failed/interrupted/cancelled`不能视为检测成功。平台现有共享审计工作区策略不变，任务执行详情只允许创建者读取。

## 验证

后端：`.venv/Scripts/python.exe -m pytest tests/test_internal_agent.py tests/test_agent_trace_workflow.py tests/test_agent_trace_rules.py -q`。隔离SQLite，provider被替换，不付费。

批次归属：`.venv/Scripts/python.exe -m pytest tests/test_internal_agent_batches.py -q`，覆盖多任务追加、统计汇总、历史版本、重试、权限、搜索分页和旧版兼容。
前端批次交互：设置 `PLAYWRIGHT_MODULE` 后执行 `node scripts/internal-agent-batches-check.cjs`。可设置 `AGENT_BATCH_START_SERVER=1` 自动启停隔离Vite服务，`AGENT_BATCH_TEST_URL` 指定地址，`AGENT_BATCH_OUTPUT_DIR` 指定截图目录。全部API响应由脚本替代，不执行实际任务。

模型服务：`.venv/Scripts/python.exe -m pytest tests/test_agent_runtime_unit.py -q`。本文件覆盖数据库fixture，仅测试provider边界，不连接MySQL。

前端：`npm run build`、`npm test -- --reporter=dot`。

本机真实验证：2026-11-11，测试数据集15，只选1条，DeepSeek完成4轮对话、4次工具请求、1次小样本检测，provider返回总用量3898 Token。任务 `AT-34a71dde69634d47`，检测 `AP-34a71dde69634d47`。验证记录保留在本地库，不批量重复付费测试。

2026-11-12 权限演示验证：任务 `AT-72c7e01a868e4ad6`，使用默认演示提示词，DeepSeek完成3轮对话、4次工具请求，2次查询成功，导出与外发各被后端实际拒绝一次，provider总用量3991 Token。未读取样本正文、未新建隐私检测、未生成导出文件、未连接邮件服务。已核对任务详情、思维路径和工具交互中的授权范围、POLICY_DENIED、未执行状态及规则发现。停止/限额之前未进入权限检查的请求保留为未知，不伪造策略拒绝。

切回main不会删除新表、任务或已产生的模型费用。合并到main前应同时评审前后端与模型服务分支。此试验实现面向当前单进程业务后端；多实例部署前需把启动恢复和后台执行迁移到具备实例归属/租约的任务worker，避免一个实例启动时中断另一个实例的任务。
