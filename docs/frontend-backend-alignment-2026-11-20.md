# 2026-11-20 前后端改动对照

核对前端提交 `e9f4013`、`2ba809d`，并与当前后端工作区已有实现对照。本次只修改后端代码、测试及本文档，没有修改前端或数据库表结构。

## 前端变化与后端处理

| 前端新增或修改 | 本次后端处理 |
| --- | --- |
| 四个轨迹视图真实模式的“导出当前JSON文件”统一打开批次赛题报告 | 扩展现有 `GET /api/v1/compliance/agent-traces/batches/{batch_id}/competition-report`，兼容 OpenAI Messages、LangChain、智能体轨迹 JSONL 和混合批次；保留赛题报告结构及原文权限 |
| 报告包含统计、思考与风险、工具与完整性、格式化预览及两种下载 | 普通样本提供相同 `A1–C5` 段落和 `A4` 汇总，下载仍使用现有 `data.jsonl`；无需新增下载端点 |
| 证据弹窗读取完整原文及前后文，直接显示字符串 `sourceRef` | 仅 `section=evidence` 的 `source_ref` 改为字符串或 null；新增 `source_location` 保留结构化来源，其他接口来源对象不变 |
| 事件描述支持悬浮全文、选择和复制 | 新导入保存完整 `description`；历史描述确为完整原文前512字符时在查询中恢复全文，独立描述保留 |
| 证据摘要支持悬浮全文、复制 | `evidence_summary` 返回未驳回证据的标签与完整命中文本，按稳定证据顺序去重拼接；默认脱敏，无风险为空字符串 |
| 文件/空批次删除与确认弹窗 | 上一轮后端已实现统一删除接口、权限、版本、幂等、解析状态检查及无引用文件清理，本次保留并回归 |
| 自动定位首个风险，工具页规则发现跳到真实事件 | 复用已有 `first_risk_event_id`、selection、graph_locator、graph 分段接口，无需新端点 |
| 时间列显隐、推理原文、工具总次数 | 上一轮已提供 `has_event_time`、`reasoning_original`、`tool_call_summary`，本次回归 |
| 全链路任务分页提取为 `listTaskPage` | `/tasks?page&page_size=100` 已支持；新增超过100条任务的分页契约测试，不改业务接口 |
| 评估快照请求提取为 `auditSnapshotHttp` | 现有 POST/GET `/evaluation/audit-snapshots` 已支持；运行现有快照测试，无需修改接口 |
| 返回按钮、标题、样式、图节点效果 | 前端交互，无需后端调整 |

## 报告兼容与数据依据

- 读取固定快照的事件、调用和持久化审计结果；GET 不重新运行安全规则、不调用模型、不改写快照。
- 赛题样本继续读取已保存 `competition_report`；普通样本使用 `analysis_version=batch-report-v1`、`report_kind=ordinary`。混合批次沿用同一 JSONL 外壳，每行标识其版本。
- 工具统计按完整 TraceCall 记录计数，含失败、拦截、未知状态；空名称归入 `unknown`。统计不受页面样本筛选和分页影响。
- 普通样本优先使用唯一的原始样本标识，缺失或重复时使用稳定 trace_id；保留赛题 trajectory_id 和末行 `aggregate`，避免名称冲突。
- 普通样本步骤采用事件序号。工具返回上的风险关联到请求步骤，保留风险事件真实 event_id、event_ordinal 和来源引用。非工具风险没有伪造的工具 step_id，使用 field/source_ref 显示来源。
- 同一源消息产生多个事件时，推理只计一次，并保存关联 step_ids；缺失Token、推理或完成状态如实返回“未提供”。未知审计结论显示“未完成审计”。
- 重复请求和运行异常使用已保存 operation/efficiency 发现，不合并成安全风险。报告中的完整原文仍要求原文查看权限。
- `source_ref` 展示示例：`file_x · 记录 1 · messages[2].tool_calls[0] · 行 1`；`source_location` 保存原来源对象，不引入服务器磁盘路径。

## 验证

274 项后端回归通过，2 项已知用例排除：`test_source_filter_does_not_change_ingest_batch_summary`、`test_incremental_publish_keeps_old_snapshot` 存在既有未定义 `ANTHROPIC` 夹具错误。另有现有 FastAPI TestClient/httpx 弃用警告。

覆盖三类普通导入、混合赛题批次、真实赛题样本、稳定标识、完整参数/返回、推理去重、工具失败、非工具风险、零值Token、缺失信息、权限、历史快照、查询不写库、全文恢复和脱敏；同时回归删除、图定位、统计、导出、任务分页、评估快照。

测试使用隔离数据库和本地文件，未将单元/HTTP契约测试表述为浏览器与生产后端的端到端联调。

## 前端仍需注意

`RuleFindings.vue` 仍主要依赖版本号和空列表判断空状态，没有消费后端已有的 `rule_audit.result_state`、`display_message`。因此“已完成零命中 / 等待审计 / 结果缺失”的三态展示仍需前端接入，不能仅靠后端准确区分显示。该项不在本次后端修改中伪装完成。

报告弹窗标题与下载名仍使用“赛题二 / submission”，现在普通批次也能读取；文案属于前端，按约束保持不动。已保存数据确实没有的推理、证据全文或运行状态不会补造。
