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

### 模型补查与联调

在上传解析任务的 input 中填写可选的 audit_strategy。省略或填 default 时仅运行平台规则。
选择 deepseek-4.1-flash 时用 DeepSeek 补查，选择 internlm-9b 时用书生补查。每条轨迹先运行规则，
规则先审计整批并分段持久化，暂存快照不可查询或导出。模型随后读取完整的规则结果，
返回 JSON 结论、步骤号、片段编号及逐字引文。原文核验通过的风险标为 other。
整批补查完成才发布合并快照；模型不可用或补查未完成时发布规则快照。
自任务受理开始计时，22 小时 55 分钟停止等待审计，预留 5 分钟发布。独立检查器可以在
模型请求仍等待时发布规则快照；规则阶段到点则发布已持久化的部分。
页面在发布前继续读取既有快照，截止原因和实际完成数量留在后台快照元数据。
工具失败及重复调用仍由平台规则判定。

后端通过 .env 或容器运行时环境变量接收以下配置，模板见 [.env.example](../.env.example)。

| 配置 | 本机开发 | 正式送测 |
| --- | --- | --- |
| AGENT_TRACE_DEEPSEEK_BASE_URL、AGENT_TRACE_DEEPSEEK_MODEL、AGENT_TRACE_DEEPSEEK_API_KEY | 官方 Base URL 为 https://api.deepseek.com，模型 ID 为 deepseek-flash，注入官方 Key | 填内网 DeepSeek 服务或兼容网关的地址、模型 ID 和鉴权信息 |
| AGENT_TRACE_INTERNLM_BASE_URL、AGENT_TRACE_INTERNLM_MODEL、AGENT_TRACE_INTERNLM_API_KEY | 使用 A100 实际 9B 地址，或已建立 SSH 隧道的 http://127.0.0.1:本地端口/v1 | 填书生 9B 服务的地址、模型 ID 和鉴权信息 |

Base URL 写接口前缀，例如 LMDeploy 使用 /v1。解析服务在此前缀下请求 /chat/completions。
从实际运行后端的进程或容器请求同一前缀下的 /models，并用一条测试消息核对模型 ID、
鉴权和 JSON 响应。在同一 Docker 网络的不同容器之间使用服务名，跨机器使用可达的内网地址。
127.0.0.1 指后端自己的网络空间。现有 A100 启动脚本的 1221 对应 InternLM3-8B，
1222 对应 Qwen，书生 9B 的实际版本、ID 和端口以交付服务的配置及探测结果为准。

请在后端运行环境注入真实 Key。配置缺失、端点不可用或结论校验失败时，当前轨迹按规则结果
展示和导出，整批回退原因记录在快照的 audit_completion 中。服务默认连接超时为 5 秒，
单次读取等待为 45 秒，每个后端进程最多并发 2 次模型请求。重新解析时可以重新选择策略。

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
当前视图生成 JSON，结果包生成 ZIP，统计样本支持完整 JSON。除下述新版思维路径结果包外，ZIP 按请求包含标准化 JSON、离线HTML、证据索引、
错误报告、统计摘要、CSV 和 manifest。执行页 JSON 聚焦选中样本，清单保存批次上下文与请求内容范围。
产物冻结快照和选择，执行统一脱敏及包内一致匿名化；manifest 保存来源说明、范围及文件哈希。
票据5分钟有效、产物24小时保留。轨迹审计不提供 PNG 导出，无需配置中文字体。

### 思维路径：三个 JSON 文件的批次结果包

真实 HTTP 模式下，思维路径页面的“导出结果包”继续发送现有
`POST /tasks/execute` 请求：`capability_code=agent_trace_export`，
`view=execution, export_kind=package, format=zip`。无需修改或重新构建前端。

新建任务由服务端在 `Task.config.execution_package_version` 保存
`execution-risk-package-v1`，生成 `agent-trace-execution.zip`，恰好包含：

| 文件 | 内容 |
| --- | --- |
| `submission.json` | 与相同快照的批次报告接口一致的 JSON 数组，包含全部样本的 A1—C5 和 aggregate 汇总。 |
| `first-risks.json` | 全部样本各自最早风险步骤的合并结果。 |
| `all-risks.json` | 全部样本全部安全风险，按步骤分组、升序排列。 |

三个文件使用 UTF-8、中文不转义、两空格缩进，保留原始轨迹编号和完整原文；
不包含 manifest 或第四个文件。只对这个新任务分支使用原文导出，其他视图和
单独 JSON 导出保持原有脱敏/匿名化策略。没有服务端版本标记的旧任务继续生成原有布局；
旧产物不改写。客户端传入同名字段不能改变服务端版本选择。

导出固定请求的批次与快照，始终覆盖整个批次，不受当前样本、分页、语言或风险筛选限制。
即使前端保留了旧的 scope/contents 参数，服务端仍按三文件批次结果包执行；
原请求结构及标识合法性继续校验。

两个风险文件均为数组，每条记录只包含 `trajectory_id` 和 `B3`，没有 `results` 外层：

```json
[
  {
    "trajectory_id": "sample-1",
    "B3": {
      "指标": "思维链可视化质量③：首次风险行为步骤、片段和原文证据",
      "risks": [
        {
          "step_id": 3,
          "risk_quote": "已保存的命中片段",
          "evidence": [{"step_id": 3, "quote": "已保存的完整原文证据"}]
        }
      ]
    }
  }
]
```

`first-risks.json` 使用上述指标，`all-risks.json` 的指标固定为
“第一/二/三次风险行为步骤、片段和原文证据”。无风险样本也保留，`risks=[]`；
两个风险文件没有 aggregate。样本顺序和轨迹 ID 与 `submission.json` 共用同一构造逻辑，
支持赛题、普通和混合批次。

全部风险读取固定快照已经保存的安全发现和风险证据，不从首次风险报告反推，也不执行新规则。
同一步的不同片段去重后用两个换行连接；证据按 `(step_id, quote)` 去重、保留首次出现顺序。
优先使用已保存的完整 `source_evidence`，否则使用已保存命中片段，不补造原文。
已明确驳回的证据排除；运行错误、重试和重复不算安全风险。
缺少审计完成状态、发现集合或必要风险依据时任务失败并给出原因，不伪造空的成功结果。

优先使用已保存赛题步骤；普通格式按页面的动作步骤映射。共享推理等不能唯一定位的内容，
保留 `step_id=null`，按源事件分别分组并排在已定位步骤后；只有未定位风险时，
首次风险文件取第一组未定位风险。缺失片段用空字符串、缺失证据用空列表，不生成解释替代证据。

原文权限与导出权限在建单、执行、票据和下载时检查；执行时重新读取操作者当前状态及角色，
下载还校验任务归属、来源授权、产物期限和 SHA-256。账号停用或降为只读后不能继续取得原文包。
不修改数据库表结构，不升级风险规则，不重写历史快照。前端“示例演示”模式在浏览器生成 ZIP，
仅修改后端不会改变该模式。

验证：`uv run pytest tests/test_agent_trace_execution_package.py -q`。

## 使用与验证

1. 重启后端并刷新前端，关闭“示例演示”。
2. 接入页点击“新建批次”，上传文件，再点击“开始解析”。
3. 其他三页沿用同一批次、快照和样本；无记录的字段显示未提供，未审计不等于无风险。
4. JSON/ZIP 使用服务端冻结的快照；新版思维路径三文件 ZIP 保留原文，其他任务式导出默认脱敏。

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

解析发布新快照前先执行规则，上传解析任务按所选策略调用模型补查。已有 upload/internal_model 批次由后台按规则升级到新快照，
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
