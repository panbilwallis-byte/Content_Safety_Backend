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

`trace-rules-v2` 与 `competition-v2` 共用安全发现。赛题 B2、统一风险标签、统计和导出保持一致；B3.risks 保存全部安全发现，B3.first_risk_step 标记首次风险步骤。样本摘要提供 risk_steps（去重升序）和 first_risk_step，规则发现提供 competition_step、message_index 和可定位时的 call_index。未命中安全规则且源审计未声明风险时判无风险；错误、重试、重复调用单独归类。

重启后端后，既有后台任务会逐批从保存的源记录重新审计，并发布新快照；无需重新上传文件。页面重新查询后使用新快照。历史快照、已导出文件保持原结论，报告只读取快照中保存的分析，不在查询时用新规则重算旧结果。

B2/B3、C2/C3 使用有限的证据规则，包括明确目录限制、已发生的授权绕过、明确超时及命令错误。未命中规则不代表全面安全；该实现不是通用语义审计模型。正常连通性否定结果、目标要求触发的崩溃、源码中的错误字符串不直接视为失败；参数相同也不直接判定重复。工具配置、usage、metadata 未提供不列入赛题完整性错误。

运行真实文件数据库/API 集成验证（隔离 SQLite 数据库）：

```powershell
$env:SCORING_SAMPLES_PATH='C:\Users\杨\Desktop\3.1赛马数据\samples.jsonl'
.venv\Scripts\python.exe -m pytest tests/test_agent_trace_competition.py -q
```
