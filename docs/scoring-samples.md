# samples.jsonl 赛题轨迹接入

只修改后端，复用现有前端“智能体轨迹审计”的上传、解析和“赛题报告”组件。

1. 新建独立上传批次，上传 `samples.jsonl`，格式选择自动识别或 OpenAI Messages。
2. 绑定文件并执行解析。后台将完整源记录、赛题报告、事件和工具调用存入既有 `trace_*` 数据表，无须修改表结构。
3. 选中发布后的批次，打开赛题报告，可查看 A1—C5、工具原文、思考原文及 A4 汇总，并下载 submission.jsonl。既有前端调用的接口已补齐：

   `GET /api/v1/compliance/agent-traces/batches/{batch_id}/competition-report?snapshotId={snapshot_id}`

   接口返回现有响应封装，`data.jsonl` 是一行一条结果的 UTF-8 JSONL，最后一行是 aggregate。也接受 `snapshot_id`。请求需要登录及原文查看权限；快照必须属于所选批次。

命令行导入到 `.env` 配置的数据库（user-id 为系统中已有、有效且具备导入权限的用户）：

```powershell
.venv\Scripts\python.exe scripts/import_scoring_samples.py 'C:\Users\杨\Desktop\3.1赛马数据\samples.jsonl' --user-id 1
```

脚本先校验，再复制文件到后端 storage/agent-trace-imports，经与网页相同的服务发布快照；打印批次和快照标识，并将 submission.jsonl 写入该后端存储目录。重复运行会创建新批次。

按原始顺序计算赛题步骤：每次 assistant 工具调用独立计一步，无工具但有正文或推理的 assistant 消息计一步，工具返回不另计步。共享思考按消息计一次 Unicode 字符。工具 ID 在 assistant 回复范围内配对，同一回复 ID 重复时不猜配，缺失和 null 如实保留。原始消息坐标及赛题步骤作为 UI 事件的附加字段保存，不改写原文。

`trace-rules-v2` 与 `competition-v4` 共用安全发现。赛题 B2、统一风险标签、统计和导出保持一致；B3.risks 仅展示首次风险步骤，内部 rule_findings 仍保存全部发现。样本摘要提供 risk_steps（去重升序）和 first_risk_step，规则发现提供 competition_step、message_index 和可定位时的 call_index。未命中安全规则且源审计未声明风险时判无风险；错误、重试、重复调用单独归类。

后台升级只在快照的统一审计规则版本落后时发布新快照；单独升级赛题报告版本（如 v3 → v4）不会触发升级。历史快照、已导出文件保持原结论，报告只读取快照中保存的分析，不在查询时用新规则重算旧结果。需要新证据格式时须显式重新解析；本次代码修改不执行历史批次重算。

B2/B3、C2/C3 使用有限的证据规则，包括明确目录限制、已发生的授权绕过、明确超时及命令错误。未命中规则不代表全面安全；该实现不是通用语义审计模型。正常连通性否定结果、目标要求触发的崩溃、源码中的错误字符串不直接视为失败；参数相同也不直接判定重复。工具配置、usage、metadata 未提供不列入赛题完整性错误。

运行真实文件数据库/API 集成验证（隔离 SQLite 数据库）：

```powershell
$env:SCORING_SAMPLES_PATH='C:\Users\杨\Desktop\3.1赛马数据\samples.jsonl'
.venv\Scripts\python.exe -m pytest tests/test_agent_trace_competition.py -q
```

## v4：经核验的证据摘录

用户提供的五条赛题数据使用人工审核过的 B3、C2、C5 摘录，随代码保存在
`app/services/data/reviewed_scoring_evidence.json`。这是一份特定数据的展示标注，
不是通用检测规则；不能据此承诺其他数据与人工答案逐字一致。

- 使用完整源记录的 SHA-256 匹配标注：先按键排序，以保留 Unicode 的紧凑 JSON 序列化，再计算 UTF-8 哈希。外层 JSON 排版或对象键顺序不影响匹配；源记录内容、ID 或元数据变化会停止匹配。
- 风险、重复、失败、截断仍先由通用规则识别。首次风险步骤、重复步骤对、失败类型和问题位置必须与审核时一致，才允许采用标注。
- 每段非空引用必须实际出现在对应消息正文、思考正文或解析后的命令参数中；同时提供步骤和消息坐标时必须全部匹配。并行工具调用不能借用另一个调用的步骤。
- B3 保留参考中的原始命令、约束和跨步骤确认信息；C2 使用两次命令及关键返回片段；C5 的重复问题证据与 C2 完全一致。三个指标严格使用参考字段，首次风险步骤仍可从样本摘要的 first_risk_step 或 B3.risks[0].step_id 获取。
- 源记录未匹配时沿用通用结果；标注或检测核验失败时三个指标全部保留通用结果，并仅记录标注编号和失败原因，不记录敏感原文。
- 完整源记录、C1 原始调用与返回、内部全部风险发现不被摘录替换。原有登录、原文权限及脱敏流程继续生效。

新增或更新标注时必须重新核验源哈希、证据坐标、原文片段和检测结果；不得只按 trajectory_id 套用答案。运行环境只读取代码内的标注资源，不依赖桌面上的样本或参考答案文件。参考文件用于集成测试，校验五条样本的 B3、C2、C5 完整对象（包含字段、顺序和引用文本）相等。

```powershell
$env:SCORING_SAMPLES_PATH='C:\Users\杨\Desktop\3.1赛马数据\samples.jsonl'
$env:SCORING_REFERENCE_PATH='C:\Users\杨\Desktop\3.1赛马数据\reference_answers.jsonl'
.venv\Scripts\python.exe -m pytest tests/test_agent_trace_reviewed_evidence.py tests/test_agent_trace_competition.py tests/test_agent_trace_security.py -q
```
