# 异常复核与风险处置

这两个模块接收模型服务已经保存的检测结果，将人工复核和处理记录保存在业务后端。
发布时生成派生样本、数据版本清单和谱系；原始样本及输入版本保持不变。
仅提交或审批不会生成新数据版本，只有调用生成版本／发布接口才会写入派生版本。

## 接入与配置

- 所有接口需要平台登录 `Authorization: Bearer <平台令牌>`。
- 同时需要 `X-Model-Access-Token: <模型服务令牌>`，后端使用它读取所属检测结果。
- `LLM_GOVERNANCE_ORIGIN` 指定模型服务源地址，默认 `http://127.0.0.1:8001`，不包含 `/api/v1`。
  沿用当前部署配置，需要时自行设置该变量；合并代码不会修改 `.env`。
- 新增 `anomaly_reviews`、`risk_dispositions` 两张表。模型随路由导入并注册到 SQLAlchemy 元数据，
  正常启动时由现有 `Base.metadata.create_all()` 创建缺少的表；不调整已有表结构、不回填旧数据。
- 模型服务需要提供 `/api/v1/data-governance/anomaly-results/{result_id}`、
  `/api/v1/data-governance/risk-results/{result_id}` 及对应样本详情接口。
  这些读取接口已存在于当前模型服务代码；本轮只用模拟响应验证，未启动真实服务联调。

## 异常复核

前缀：`/api/v1/data-governance/anomaly-reviews/{result_id}`。

| 方法与路径 | 行为 |
| --- | --- |
| `GET /state` | 返回可编辑状态、复核记录及待发布变更集 |
| `POST /samples/{sample_id}/candidates/{action}` | 提交、批准、拒绝、撤回候选修复；action 为 `submit/approve/reject/withdraw` |
| `POST /change-set/remove` | 从待发布变更集中移除批准项 |
| `POST /change-set/validate` | 校验当前变更集并返回发布令牌 |
| `POST /change-set/generate-version` | 用校验返回的令牌生成数据版本 |

候选操作只接受 `candidate_id`、`input_sample_revision_id`；字段修改从模型保存的结果读取，
不接受客户端提交修改正文。目前仅支持 `anomaly-basic-v2`、`anomaly-basic-v2.1` 方案的单个
`topic_label` 修改。需要具有写权限，且输入版本仍为数据集当前版本。
同一样本只能有一个已批准修复；同范围不同结果的已批准项可进入同一变更集。

## 风险复核与处置

前缀：`/api/v1/data-governance/risk-dispositions/{result_id}`。

| 方法与路径 | 行为 |
| --- | --- |
| `GET /state` | 返回复核、处置记录和已发布版本 |
| `POST /samples/{sample_id}` | 提交、确认、调整、驳回风险，提议或审批处置 |
| `POST /preview` | 校验待发布处置，返回替换／排除数量及发布令牌 |
| `POST /publish` | 使用预览令牌发布派生版本 |

样本操作包含 `action`、`input_sample_revision_id`，按操作需要填写 `note`、`level`、
`treatment`、`replacement_text`。action 为 `submit/confirm/adjust/dismiss/propose/approve/return`，
level 为 `NOTICE/LOW/MEDIUM/HIGH`，处置为 `redact/correct/exclude`。
替换正文必须有实际变化；替换后的派生样本去掉绑定旧正文的参考标签，等待重新标注。
排除只影响新版本清单，不删除原样本。排除全部样本时拒绝发布空版本。

## 一致性与错误

后端核对模型令牌、结果范围、样本所属版本、修订号；风险处置还核对版本清单哈希。
只读账号不能写入，旧版本不能继续修改。模型服务不可达返回 `502`；越权返回 `403`；
版本、修订或审批状态冲突返回 `409`；不合法输入返回 `422`。
审批记录改变后旧发布令牌失效；同一令牌的重复发布返回已生成版本。
发布记录、任务、版本清单和谱系处于同一数据库事务，任何一步失败均回滚。

## CSV 参考标签

`reference_risk_level` 空白时按未标注处理；`reference_is_anomaly` 的“污染／未污染”转换为
`true/false`，空白按未标注处理，其他值报出源文件行号。其他列、原始文件及行号保留。
CSV 行指纹按转换后的字段计算，使缺少参考标签与空白参考标签的相同样本保持一致。

## 赛题报告比较工具

```powershell
.venv\Scripts\python.exe scripts/compare_trace_submission.py submission.jsonl reference_answers.jsonl
```

支持 JSONL、轨迹对象数组、单条轨迹对象，以及当前 `competition-report` 接口返回的
`jsonl` 包装或完整 `data` 响应。按轨迹 ID 比较结构化指标，保留工具名称大小写、参数与返回空白。
退出码 `0` 表示可自动比较的内容一致，`1` 表示有差异，`2` 表示文件格式错误。
B3/C5 的自动检查侧重步骤和定位，不能代替证据原文及充分性的人工核对。

## 本次合并边界

以当前工作区为主，加入两个复核模块、共享版本辅助逻辑、CSV 改进和比较工具。
压缩包的轨迹服务旧实现、PNG 导出、环境配置、上传文件和日志未覆盖当前版本。
当前赛题计步、节点短标签、风险定位、调用状态、重复调用、删除接口及 JSON/ZIP 导出均保留。
验证使用内存 SQLite 和模拟模型响应；未重启服务、未操作实际数据库、未修改前端及模型服务。

### 合并验证记录

新增模块、CSV、主应用路由与鉴权、发布失败回滚、报告比较、数据版本和谱系相关测试共 42 项通过。
后端完整回归结果为 506 项通过、2 项失败、1 项默认跳过，另有 2 个子测试通过。
两项失败均在 `test_risk_evaluation.py`：价值／异常准确率测试预期分母为 6，当前实现排除
不可评估样本后为 5。在内存加载合并前 HEAD 的应用代码单独重跑，同样两项失败，确认不是本轮合并引入。
本轮保留现有评估口径与测试，不修改这些无关功能。
随后设置外部赛题文件路径，单独运行五条轨迹端到端及计步测试，2 项通过；补齐了完整回归默认跳过的样本验证。
步骤数仍为 7、43、22、6、24，参考答案核对通过。
