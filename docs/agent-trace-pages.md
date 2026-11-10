# 轨迹接入、统计指标与思维路径后端

前后端共同支持外部轨迹导入、统一解析、四页查询和固定快照导出。请使用真实 HTTP 模式。
基础启动、数据库、模型来源及下载鉴权配置沿用 [工具页部署说明](agent-trace-tools.md)。
所有路径以下省略 `/api/v1`，除短期下载票据 URL 外均需要登录。

## 数据与持久化

默认只显示文件上传批次和未来内部真实调用批次。后台不再自动扫描价值分析结果生成模拟过程。
历史模拟批次保留在库中，默认从列表和直接访问中排除；只有显式设置
`AGENT_TRACE_ENABLE_LEGACY_VALUE_SYNC=true` 才恢复旧同步及历史展示，生产默认 false。
不要关闭整个 TraceSynchronizer：它继续处理上传解析、统计补算、导出及过期文件清理。

上传轨迹解析实际记录，不补造时间、Token、工具调用或风险结论。文件的 source 是来源声明，
不用于冒充本平台产生的轨迹；接入来源保存为 external_import。topic 仅为测试类别。

新增资产、批次文件绑定和统计样本表；现有快照、投影、事件、调用、证据、任务及产物表复用。
启动时通过现有 metadata.create_all 创建新表，无需变更原业务表字段。
统计投影 `statistics_v1` 与执行/工具投影 `value_tools_v1` 独立，事件和样本标识共享。
后台每次最多补算同一快照的500条统计样本，全部完成后发布统计投影，GET 不生成数据。
读取固定 batch_id、snapshot_id、projection_version；不会把历史版本偷偷切换为最新版本。

## 轨迹接入与校验

| 请求 | 功能 |
| --- | --- |
| POST `/compliance/agent-traces/batches` | 创建上传批次，body 为 `{"name":"我的轨迹批次"}` |
| POST `/files` | multipart 文件上传，返回 file_id |
| POST `/compliance/agent-traces/batches/{batch_id}/files` | 原子绑定文件及格式选择 |
| POST `/tasks/execute` | capability_code 为 agent_trace_ingest，异步解析 |
| GET `/tasks/{task_id}` | 解析进度和结果 |
| GET `/compliance/agent-traces/workspace?view=ingest` | 文件、格式示例、解析概况、校验及默认选择 |
| GET `/compliance/agent-traces/batches/{batch_id}/samples?view=ingest` | 搜索、来源/问题筛选、真实分页 |
| GET `/compliance/agent-traces/batches/{batch_id}/samples/{trace_id}?view=ingest` | 事件、标准化内容、原文、字段映射和问题 |

写入携带唯一 `X-Request-Id`；同键同内容复用结果，不同内容返回409，幂等记录保留至少24小时。
文件仅允许绑定上传者自己的资产；只读角色不能写入。价值来源批次由分析任务管理，不能绑定上传文件。

绑定示例：

```json
{"expected_version":1,"files":[{"file_id":"上传返回的ID","format_override":"auto"}]}
```

解析任务示例（expected_version 使用绑定后响应中的 batch_version）：

```json
{"capability_code":"agent_trace_ingest","name":"解析轨迹","input":{"batch_id":"批次ID","expected_version":2,"mode":"parse_pending"}}
```

支持 parse_pending、retry_failed、reparse_all；可指定 file_ids，省略则覆盖全部符合条件的文件，
不受首屏文件列表长度限制。任务冻结绑定和版本，解析中不允许编辑绑定。
每次绑定最多20个文件，单轨迹文件最多10MiB、10000条记录，UTF-8 JSON/JSONL。
支持 OpenAI messages、智能体轨迹 JSONL（sample_id/events/tool_events/tokens，与 trajectories(2).jsonl 一致）、LangChain runs 包装；并非任意厂商格式均可自动解析。
LangChain 按根运行拆分，缺父节点或循环引用拒绝；工具调用和返回按源ID关联，无法配对会记录问题。
原记录文本保留。有效记录可部分发布；全部失败保留旧快照，增量解析保留未重解析文件的样本。
统计补算故障不会改写已发布的解析成功状态，后续由同步器继续处理。

## 统计指标与 JSON 查看

- GET `/compliance/agent-traces/statistics`：批次选择、八个指标、八个图表、完整性及列表上下文。
- 样本列表和详情沿用上述 samples 路由，指定 `view=statistics`。
- 支持 language、list_filter（all/risk/missing）、page/page_size 和固定版本。
- 风险/缺字段标签只筛选列表，不改变批次聚合；完整性可重叠计数，状态分类互斥。
- 没有实际记录的指标区分 unavailable 与真实0；真实审计结论另存 actual_audit_decision。
- JSON 抽屉提供标准化 JSON、原始预览、字段映射、跨页相邻样本及完整 JSON 下载。
- JSON 分段游标按 UTF-8 字节、内容版本和哈希校验；任意 JSON 键名保真。

## 思维路径

workspace 使用 `view=execution&include=options` 或 include=dashboard；列表、详情使用 view=execution。
详情支持 overview/events/selection/graph，事件与工具页共享ID。
支持事件过滤、选择上下步、证据与来源定位、图分段以及长文本续读；selection 的文本游标按 UTF-16 单位，
与浏览器字符串偏移一致。后端 dashboard 可接收目标 trace_id 定位所在页，selection 返回目标所在事件页。
只读用户不能读取原文；权限在服务端检查，下载时再次校验账号及来源授权。

## 导出

沿用 agent_trace_export 任务、任务查询、下载票据及同源二进制下载。
现在除 tools 外支持 ingest/statistics/execution，按页面协议验证 batch/filtered/sample 范围与内容。
可生成 PNG、ZIP，统计样本支持完整 JSON。ZIP 按请求包含标准化 JSON、离线HTML、证据索引、
错误报告、统计摘要、CSV 和 manifest。执行页 PNG 聚焦选中样本，清单保存批次上下文与请求内容范围。
产物冻结快照和选择，执行统一脱敏及包内一致匿名化；manifest 保存来源说明、范围及文件哈希。
票据5分钟有效、产物24小时保留。PNG 需要中文字体，可配置 AGENT_TRACE_FONT。

## 使用与验证

1. 重启后端并刷新前端，关闭“示例演示”。
2. 接入页点击“新建批次”，上传文件，再点击“开始解析”。
3. 其他三页沿用同一批次、快照和样本；无记录的字段显示未提供，未审计不等于无风险。
4. PNG/ZIP/JSON 使用服务端冻结的快照和脱敏产物，PNG 不再截取下载时的实时 DOM。
5. 如本机没有中文字体，为 PNG 配置 AGENT_TRACE_FONT。

新增隔离数据库流程测试：tests/test_agent_trace_workflow.py、tests/test_agent_trace_trajectory.py。
涵盖默认隐藏旧模拟来源、实际文件导入、字段保留、非首屏定位、重解析、审计证据和导出。
使用项目 .venv 的 pytest；测试数据库为内存 SQLite，不修改业务数据库。

## 智能体轨迹 JSONL

第三种格式现为 `agent_trajectory`，替换 Anthropic Messages；可自动识别或手动指定。
对应外部生成的 trajectories(2).jsonl：每行一个含 sample_id、events、tool_events、tokens 的样本。
事件按 idx 排序，step_id 为分组编号，不能作为唯一事件编号。工具明细按 step_id + tool 关联，
不会将 events 与 tool_events 重复计为两次调用。保留推理原文、相对时间、原始模型与样本编号、
Token 估算标记及来源声明；success/error/blocked 分别映射为成功/失败/未执行。
拦截耗时单独保留，不计入工具实际执行耗时。topic 仅为测试类别，不生成风险审计结论。
outcome=success 映射为流程完成，不代表回答正确。Token 的 input/reasoning/output 按该采集格式的独立分量汇总。
已有 Anthropic 历史数据不迁移，新文件不再提供该格式入口。重启后端并刷新前端后生效。

## 外部风险结果协议（可选）

外部 audit 可缺省；此时仍执行后端有限规则，不按 topic 推断风险。外部评价器可提供：

```json
{"audit":{"decision":"risk_found","risk_level":"high","provider":"评估器名称","version":"v1","evidence":[{"event_idx":3,"risk_type":"privacy_leak","label":"隐私泄露","matched_text":"原始匹配片段","severity":"high","rule_id":"rule-1","confidence":0.9}]}}
```

证据 event_idx 对应原始 events[].idx；保留为外部记录的判定，平台不会把它标为自己检出的风险。
没有证据或置信度时不得补写。未知任务状态不按失败计入流程完成率；样本及汇总显示估算 Token 标记。

## 后续内部真实调用接入

app/services/agent_trace_internal.py 提供服务器端 record_model_call：真实调用结束后传入稳定调用编号、
模型名称、请求消息、实际响应、供应商 usage 和实际耗时；调用方管理事务。
同一调用编号重放不重复创建批次，可在同一事务中独立保存，返回 batch_id/snapshot_id/trace_id 供审计跳转。
当前只预留并验证这一接入口，没有自动挂接现有模型调用链，也不会调用模型或构造工具结果。
普通问答自然产生零次工具调用。未来智能体应把实际工具请求/返回写入捕获的消息，或通过统一标准化结构发布。
内部来源只允许可信服务器代码写入，没有向浏览器开放伪装内部来源的接口。

## 后端规则审计（trace-rules-v1）

解析发布新快照前执行规则；不调用任何大模型。已有 upload/internal_model 批次由后台升级到新快照，
旧快照、已提交导出和历史统计不被覆盖。后台每轮升级一个批次，后续补算统计；页面若固定旧快照，
点击“查看最新结果”。批次名称统一显示为“名称（系统编号）”。

规则分类：
- security：有明确禁止原因的请求、与明确授权不符的请求、工具返回中的提示注入线索。
- policy：源日志记录策略拒绝；缺少具体原因时不直接判越权。
- operation：工具错误/超时、运行错误、流程未完成。HTTP_403 本身不推断越权。
- efficiency：源日志重复/重试标记，以及相同工具参数再次请求；仅为线索，不宣称一定低效。

安全风险卡按样本去重；策略拒绝、运行异常、效率线索分别统计且可重叠。提示注入只判外部指令暴露，
不推断攻击成功。缺少授权时记录 unknown_permission_call_count；未命中仅代表本版有限规则未命中，不代表全面安全。
每条发现保存规则版本、事件/调用编号、证据文本、来源位置和执行结果，四页及导出共用该数据。
原文件的外部审计结论保留在 source_audit_decision，不被当成本平台规则结果。

工具轨迹可选提供 tool_events[].granted_scopes/requested_scopes（字符串数组），只有明确提供时才做权限对比；
支持授权范围中的通配符，单独的 policy 标签不当作完整授权。规则见 app/services/agent_trace_rules.py。
新增测试 tests/test_agent_trace_rules.py 覆盖类别隔离、三类格式、拒绝依据和历史快照升级。
