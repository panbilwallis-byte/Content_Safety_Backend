# 智能体风险来源溯源

执行详情接口（`view=execution`）增加 `risk_provenance`，不修改已发布快照，不调用模型。

每条链按风险请求聚合证据，包含可定位的事件节点、连线依据、实际调用结果和证据缺口。
`section=selection/events` 同样返回该字段，以便原文/脱敏切换时更新全部链路内容。

## 关联规则

- 危险请求以快照中的风险证据为准，不以测试类别或步骤颜色生成风险。
- 工具请求和返回通过同一调用的 `event_id/return_event_id` 配对，状态为 `recorded`。
- 从危险请求之前的最多 32 个事件中，查找内容同含导出、发送、读取或删除意图的任务指令，或已经由审计规则检出外部指令线索的工具返回。每类保留最近一条，关系标为 `candidate`。
- 用户指令标为“相关任务指令”，不会标为外部攻击。一般网页内容不会仅因在请求之前出现而成为攻击来源。
- 只有 `reasoning_provenance=source_log` 且意图吻合的摘要可作为中间节点；不会生成模型内部推理。
- 外部指令暴露单独保留证据链，不代表其后发生了危险行为或攻击成功。
- 所有候选影响关系均为“待人工复核”。当前没有人工复核写入接口，也没有移除片段后的对照重放，不能把候选关系升级为已验证因果。

## 显示及限制

最多展示 50 个风险节点聚合的链，返回 `total/complete` 说明是否截断。未记录的来源、摘要或调用返回均明确提示，不补造。
内容先按既有权限脱敏，再截取最多 420 字符；原文权限与执行详情接口保持一致。
前端默认展示风险链，节点点击联动步骤与日志，箭头点击查看连线依据；“记录出处”保留原始来源引用视图。
演示模式的关联单独标注，不能当作实际审计证据。

## 验证

后端：`python -m pytest tests/test_agent_trace_provenance.py tests/test_agent_trace_rules.py`。
前端：`npm test`、`npm run build`。
浏览器：启动隔离 Vite 服务（默认 1236），通过 `PLAYWRIGHT_MODULE` 指定 Playwright 包路径后运行 `node scripts/agent-trace-provenance-check.cjs`。
可设置 `PROVENANCE_TEST_URL` 和 `PROVENANCE_OUTPUT_DIR`。浏览器检查会拦截所有 API 请求，只使用演示数据。
设置 `PROVENANCE_START_SERVER=1` 时，脚本会启动并在检查结束后关闭自己的 Vite 服务。
