# 轨迹接入、统计指标与思维路径后端

仅修改 Content_Safety_Backend，复用现有前端真实 HTTP 模式。工具交互页继续可用。
基础启动、数据库、模型来源及下载鉴权配置沿用 [工具页部署说明](agent-trace-tools.md)。
所有路径以下省略 `/api/v1`，除短期下载票据 URL 外均需要登录。

## 数据与持久化

价值分析结果由后台同步器接入，一次来源任务对应一个批次；真实结果和模拟过程分开保存。
来源显示“价值分析 · 含模拟过程”，模拟分类、审计标签及调用不写回真实价值评分，也不代表实际检测结论。
上传轨迹则解析实际记录，不为没有记录的 Token、时间或审计结论补造真实值；真实零值保留。

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
支持 OpenAI messages、Anthropic tool_use/tool_result、LangChain runs 包装；并非任意厂商格式均可自动解析。
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

## 验证与当前前端限制

```powershell
.venv/Scripts/python.exe -m pytest -q --disable-warnings --maxfail=3
.venv/Scripts/python.exe scripts/check_agent_trace_live.py --http
node scripts/check_agent_trace_pages_browser.cjs
```

live 脚本签发本机验证账号的一小时会话，保存到忽略目录 storage/browser-check/session.json；
不要共享，验证后删除。浏览器脚本使用1225端口前端及8000端口后端，创建明确命名的联调上传批次。
实际 MySQL 8.0.46、127.0.0.1:3307/content_safety 已通过验证。业务价值来源接入60批次、7548样本，
另10个无可靠样本明细的历史任务保留失败诊断；模型来源未配置有效 LLM_ACCESS_TOKEN，不越权读取。
最终自动回归169项通过。2026-11-09三页浏览器复验通过15项检查、39次相关请求且无页面错误，
包含真实上传、绑定、解析、筛选、
跨页 JSON、图事件选择和 PNG/ZIP/JSON 下载。证据保存在 storage/browser-check/three-pages-browser-report.json
及同目录截图。测试使用隔离数据库，浏览器使用真实 MySQL 和 HTTP，不回退示例数据。

现有前端保持不变，因此有以下限制：

1. 必须关闭“示例演示”。
2. 当前接入页已有新建批次对话框，但没有按钮触发打开；首次通过 Swagger/API 创建上传批次，
   然后在页面选择该批次进行上传和解析。
3. 执行页没有完整透传非首屏目标及历史版本定位参数；后端精确定位能力已经实现，前端深链仍有限制。
   统计页/执行页初始化也不能依赖 URL 中的 batchId，应通过批次下拉框选择后点击查询。
4. 页面 PNG 在任务完成后截图当前 DOM；服务端规范 PNG 与该截图的范围、匿名化及字节不保证一致。
   ZIP 和完整 JSON 使用真实服务端产物。
