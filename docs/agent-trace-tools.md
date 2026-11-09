# 工具交互后端接入

本文件记录工具页首版实现。后续已增加接入解析、统计指标及完整思维路径读取，
新增能力与联调记录见 [三页后端接入说明](agent-trace-pages.md)。下文“仅工具页”等范围描述为首版历史记录。
只修改本后端；价值分析服务和 Vue 项目保持不变。

## 启动与来源

执行 `uv sync --group dev`，使用项目原有命令启动 FastAPI。
启动时由现有 `Base.metadata.create_all` 创建 `agent_trace_*` 新表，不改变原有表字段。
后台同步器立即启动，此后每 10 秒导入已完成价值任务，GET 不触发生成。

- 本库 `tasks`：`value_score/high_value_detect`，必须已保存 `result.samples` 明细。
- 模型服务：`llm_governance_*`，只接入配置的 `LLM_ACCESS_TOKEN` 归属结果，逐次检查令牌有效性。
- 不修改来源表，不通过旧价值查询接口重建样本编号。无可靠样本的汇总任务会留下失败检查点。
- 一条来源任务对应一批次；唯一约束和确定 ID 保证重复同步不会重复发布。
- 模型失败样本仍根据检查点保存失败原因，已记录评分、原文、真实零值不覆盖。

可选环境配置（写入自己的 `.env`，不要提交凭据）：

```dotenv
# 已有配置：模型服务结果的授权令牌
LLM_ACCESS_TOKEN=填写已授权令牌
# 仅在模型服务结果不在业务 MySQL 时设置；账号只需 SELECT 权限
# AGENT_TRACE_LLM_DATABASE_URL=mysql+pymysql://USER:PASSWORD@HOST:3306/DATABASE?charset=utf8mb4
# PNG 字体；Windows 自动寻找微软雅黑，Linux 自动寻找 Noto CJK/WenQuanYi
# AGENT_TRACE_FONT=/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc
# AGENT_TRACE_ARTIFACT_DIR=storage/agent-trace-artifacts
```

多实例部署必须共用 AUTH_SECRET、产物目录及相同来源配置。生产应通过同源反向代理提供
`/api/v1`；下载票据返回同源路径，不向外域转发 Bearer。

平台采用共享工作区：已登录且状态有效的用户可读已授权来源批次；只读角色不能读取原文或导出。
不是逐任务创建人隔离。导出任务及产物仅创建者可读；下载时再次检查用户状态、当前角色、
模型来源令牌、固定快照及文件哈希。勿将未打算共享的模型服务令牌配置到工作区。

## 真实结果与生成过程

评分原始结果保存在 `agent_trace_samples.result`，展示过程独立保存在事件、调用、证据等表。
缺过程时使用固定场景，来源显示“价值分析 · 含模拟过程”。首场景为 18 事件、7 调用、
4 工具、5 成功、1 执行失败、1 权限拒绝，另含两次低效调用；真实审计结论保持 unknown。
同一次导入不会随刷新、重启改变。模拟过程不是实际调用或风险证据，不写回价值评分。

若来源样本提供 `execution={events:[], calls:[], ...}`，按规范事件和调用 DTO 保存实际过程，
包括真实零调用；归属、唯一性和事件关联校验失败时，整个批次不发布。
数据原点、生成规则版本、字段来源与来源任务标识随记录持久化。

## HTTP 查询

所有 JSON 使用既有 envelope 和 snake_case；任意 JSON 内容采用字符串保真。
以下省略 `/api/v1`，均需平台 Bearer。

| 请求 | 用途 |
| --- | --- |
| GET `/compliance/agent-traces/workspace?view=tools&include=options` | 批次列表；指定 batch_id 返回 candidate_scope 和 sample_options |
| GET `/compliance/agent-traces/batches/{batch_id}/samples?view=tools` | 稳定分页、样本编号搜索 |
| GET `/compliance/agent-traces/batches/{batch_id}/samples/{trace_id}?view=tools` | overview/calls/selection/sequence，支持 include_raw |
| GET 同 workspace，`view=execution&include=dashboard` | 固定批次快照、按 trace_id 定位目标所在页 |
| GET 同 samples/detail，`view=execution` | 样本分页；overview/events/selection/graph |

后续请求固定 snapshot_id/projection_version。不存在、跨范围、回收版本分别按 404/409/410
处理，不偷偷切换数据。列表默认 tools=50、execution=6；调用页默认20；时序段200；图段500。
`section=selection` 工具页要求 call_id，执行页要求 event_id。执行页 selection 和 events 均双返回。
overview 和显式 sequence 均接受 sequence_cursor；游标签名并绑定操作者、scope、样本和种类。

`workspace.warnings` 提供接入检查点诊断。`agent_trace_ingest_checkpoints` 中 `system:*`
表示来源可用性，其余为逐任务接入结果，不包含数据库密码或完整原文。

## 导出

复用 `POST /tasks/execute`，能力 `agent_trace_export`，目前只接受 view=tools，
scope.type=sample 且一个 trace_id。写入必须提供 X-Request-Id：同键同内容返回原任务，
同键不同内容返回409，至少保留24小时，命中也重新鉴权。

- `export_kind=view, format=png, contents=[tools_png]`：真实 PNG 规范报告，需要中文字体。
- `export_kind=package, format=zip`：支持 normalized_json、evidence_index、manifest，manifest 必选。
- 强制 standard 脱敏和包内一致匿名化；原始日志不作为独立文件导出。
- 通过 `GET /tasks/{task_id}` 查询，文件已落盘才能 succeeded；失败提供 error_message。
- POST `/compliance/agent-traces/artifacts/{artifact_id}/download-tickets`，body={}，返回5分钟票据。
- 文件 GET 不带 Bearer，使用票据 URL 校验。只有这一条指定下载路由可用票据授权。
- 产物24小时后过期，后台清理字节；超过一小时未结束的导出标记中断，pending 任务可重启续办。
- manifest 列出实际文件哈希、版本和模拟来源；不输出反向匿名映射。

## 前端现有限制（本次未修改）

1. 需在页面开关关闭示例模式，否则不会请求本模块；不要将 Mock 成功当作真实联调。
2. 工具定位路由携带完整 scope，但执行页初始化没有完整透传历史版本、非首屏目标。
   后端直接 API 支持精确定位，浏览器这些深链场景仍受前端限制。
3. 当前视图最终由 ViewExportStatus 在任务完成时截图当前 DOM；不会下载后端 PNG。
   页面变化可能导致截图与任务范围不同；本地截图也不执行后端匿名化，不能作为后端产物证明。
4. ZIP 仍使用真实票据和后端文件。本模块没有实现另外三页的完整功能或完整批次导出。

## 验证

`uv run --group dev pytest tests/test_agent_traces.py -q`

测试使用隔离 SQLite、真实 FastAPI 请求和 PNG/ZIP 字节，不写生产数据库，不调用模型。
上线需另外验证实际 MySQL 来源读取、字体和产物存储、反向代理，以及真实账号浏览器流程。

## 2026-11-08 实际 MySQL 与浏览器联调记录

已通过原后端 `.env` 连接 `127.0.0.1:3307/content_safety`（MySQL 8.0.46），正常启动应用并创建审计表。
从70条已完成业务价值任务中导入60份有已保存样本明细的结果：60批次、7548样本、135864事件、52836调用。
其余10条仅有汇总或缺可靠明细，保留接入失败诊断，没有为其伪造样本归属。
这是实际 MySQL 中持久化的数据；工具执行过程仍按本模块规则标记为模拟，不能据此认为发生过真实工具调用。

模型库另有4份价值结果，但当前后端没有配置 `LLM_ACCESS_TOKEN`，未读取这些结果。
配置正确且有效的授权令牌后，后台同步器才能接入；当前业务库来源已经可用。

验证入口：

```powershell
.venv/Scripts/python.exe scripts/check_agent_trace_live.py --http
node scripts/check_agent_trace_browser.cjs
```

第一条脚本使用当前数据库中已有有效操作账号签发一小时验证会话，不修改账号口令，
并创建PNG/ZIP导出任务验证真实HTTP下载与SHA-256。会话仅写入忽略目录
`storage/browser-check/session.json`，不要共享或提交，浏览器验证完成后可删除。
第二条脚本需要 Playwright、已安装 Edge 和1225端口的前端服务器；Playwright 模块目录可用
`CODEX_BROWSER_MODULES` 指定，默认使用本机 Codex runtime。

联调采用当前前端源码的原样临时副本，保存在后端 `storage/browser-check/frontend`，
没有修改 ContentSecurityVue 或模型服务。前端临时副本运行在1225端口，代理到实际8000端口后端，
明确切换真实HTTP模式，没有拦截请求伪造响应，也没有失败回落Mock。

浏览器验证24次相关请求均为成功响应、无页面脚本错误，覆盖：批次初次查询、样本切换及待提交状态、
远程样本搜索、批次切换、时序和调用列表、原始事件抽屉、首屏样本思维步骤精确选中、
浏览器PNG下载及服务端ZIP票据下载。历史版本、非首屏样本深链限制仍按前述说明保留。

结果位于 `storage/browser-check`：

- `mysql-http-report.json`：实际数据库数量、来源诊断、HTTP及导出哈希校验结果。
- `browser-report.json`：页面操作结果、相关请求状态；不包含访问令牌。
- `tools-real-mysql.png`、`tools-second-sample.png`、`execution-location.png`：浏览器截图。
- `server-report.png/zip`、`browser-current-view.png`、`browser-result-package.zip`：可打开的文件证据。

真实下载测试中发现 Uvicorn 默认访问日志会记录票据查询参数，已添加下载路由访问日志过滤器，
后续日志只显示 `t=[REDACTED]`，不改变票据校验和下载行为。

截图复核还发现事件标识中的纯数字片段可能被手机号规则误脱敏，已修正为保留规范系统标识、
继续脱敏正文及任意JSON参数中的敏感值。浏览器额外验证STEP-16选中后仍可用原event_id读取详情，
避免图节点、日志和接口标识不一致。此修复不改变数据库原始记录。
