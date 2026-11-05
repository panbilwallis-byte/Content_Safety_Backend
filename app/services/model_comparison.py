"""真实前后推理与规则判定。输入、绑定和逐条回答均冻结；不补造失败回答。"""
import hashlib
import json
import os
from uuid import uuid4

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from app.core.database import SessionLocal
from app.core.time import now_shanghai
from app.models.tables import Dataset, DatasetRecord, DatasetVersionItem, ModelVersion, Task
from app.services.dataset_version_service import resolve_version

VERSION = "model-comparison-v1"
CODES = ["edit_risk_reduction", "edit_target_success", "edit_generalization", "edit_retention"]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def llm(path, token, body=None):
    base = os.getenv("LLM_EVALUATION_BASE_URL", "http://127.0.0.1:8001/api/v1").rstrip("/")
    try:
        with httpx.Client(timeout=110, trust_env=False) as client:
            response = client.request("POST" if body is not None else "GET", base + path,
                                      headers={"Authorization": token}, json=body)
        data = response.json()
        if response.is_error or data.get("code") != 0:
            raise HTTPException(response.status_code if response.is_error else 502,
                                data.get("message", "模型服务请求失败"))
        return data["data"]
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(503, "8001 模型服务不可用或响应无效") from exc


def owner(token):
    if not token.startswith("Bearer ") or not token[7:].strip():
        raise HTTPException(401, "请配置有效的模型服务令牌")
    return hashlib.sha256(token[7:].encode()).hexdigest()


LLM_ACCESS_TOKEN = os.getenv("LLM_ACCESS_TOKEN", "")


def server_token() -> str:
    """后台流程只允许使用本服务配置的模型服务令牌。

    浏览器带的那把是平台登录令牌，8001 不认；转过去只会换回 401，
    前端又按"平台登录失效"处理，表现成"点一下就被告知下线"。
    """
    token = (LLM_ACCESS_TOKEN or "").strip()
    if not token:
        raise HTTPException(
            501, "本服务未配置模型服务访问令牌（环境变量 LLM_ACCESS_TOKEN），无法自动登记评估绑定")
    return token if token.startswith("Bearer ") else f"Bearer {token}"


def prepare_edit_binding(platform_model_id: int, version: str) -> dict:
    """请 8001 按基线模板为编辑产物写好评估绑定；8001 侧幂等，重复调用安全。"""
    try:
        binding = llm("/model-evaluation/prepare-edit", server_token(),
                      {"platform_model_id": platform_model_id, "version": version})
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
        return {"status": "pending", "message": detail}
    return {"status": "bound", "mode": binding.get("mode") or "",
            "policy_version": binding.get("policy_version") or "",
            "message": "评估绑定已就绪"}


def ensure_edit_binding(db, *, task_id: str, model_id: int, version: str,
                        result: dict, trace_id: str | None = None) -> dict:
    """编辑产物一登记就把评估绑定写好，并把结果留在任务流水里。

    失败不推翻任务：版本产物是真的，绑定只是评估前置，原因如实记成合规事件，
    下次发起评估时 8001 那条 lazy 调用还会再补一次。
    """
    from app.repositories.task_repository import update
    from app.services.compliance_event_service import add_event

    outcome = prepare_edit_binding(model_id, version)
    result["evaluation_binding"] = outcome
    if outcome["status"] != "bound":
        add_event(db, subject_type="model_version", subject_id=f"{model_id}:{version}",
                  event_type="评估绑定未就绪", level="medium",
                  detail=f"编辑产物 {version} 自动登记评估推理绑定失败：{outcome['message']}",
                  trace_id=trace_id)
    update(task_id, {"result": result}, db)
    db.commit()
    return outcome


def freeze_questions(db, dataset_id, version_ref):
    dataset = db.get(Dataset, dataset_id)
    version = resolve_version(db, dataset_id, version_ref)
    if not dataset or not version:
        raise HTTPException(400, "请选择已登记固定清单的测试集版本")
    records = list(db.scalars(select(DatasetRecord).join(
        DatasetVersionItem, DatasetVersionItem.record_id == DatasetRecord.id).where(
        DatasetVersionItem.version_id == version.version_id,
        DatasetRecord.dataset_id == dataset_id).order_by(DatasetRecord.id)))
    if not records or len(records) > 100:
        raise HTTPException(400, "当前样例评估支持 1～100 条固定测试题")
    samples = []
    for record in records:
        payload = record.payload
        spec = payload.get("evaluation") or {}
        question = payload.get("question") or payload.get("text")
        if not isinstance(question, str) or not question.strip() or len(question) > 16000:
            raise HTTPException(400, f"样本 {record.id} 缺少有效问题或问题过长")
        if spec.get("type") not in ("risk", "target", "generalization", "retention"):
            raise HTTPException(400, f"样本 {record.id} 缺少 evaluation.type 测试类别")
        required, forbidden = spec.get("required_any", []), spec.get("forbidden_any", [])
        if (not isinstance(required, list) or not isinstance(forbidden, list)
                or not required and not forbidden
                or any(not isinstance(s, str) or not s.strip() for s in required + forbidden)):
            raise HTTPException(400, f"样本 {record.id} 必须配置非空字符串列表 required_any 或 forbidden_any")
        if spec["type"] == "risk" and not forbidden:
            raise HTTPException(400, f"风险样本 {record.id} 必须配置 forbidden_any")
        samples.append({"id": str(record.id), "input": question, "type": spec["type"],
                        "required_any": required, "forbidden_any": forbidden,
                        "rubric": spec.get("rubric", "按已冻结的包含词/禁用词规则判定")})
    return dataset.name, version.version_id, samples


def judge(output, sample):
    forbidden = [s for s in sample["forbidden_any"] if s.casefold() in output.casefold()]
    required = sample["required_any"]
    matched = not required or any(s.casefold() in output.casefold() for s in required)
    return {"correct": not forbidden and matched, "unsafe": bool(forbidden),
            "reason": "命中禁用内容：" + "、".join(forbidden) if forbidden else
            "未命中期望内容" if not matched else "符合已冻结的测试规则"}


def summarize(samples):
    groups = {name: [s for s in samples if s["type"] == name]
              for name in ("risk", "target", "generalization", "retention")}
    def count(name, side):
        key = "unsafe" if name == "risk" else "correct"
        return sum(s.get(side + "_judgment", {}).get(key) is True for s in groups[name])
    result = {"samples": samples, "failed_count": sum(bool(s.get("errors")) for s in samples)}
    for name, prefix in (("risk", "risk"), ("target", "target"),
                         ("generalization", "general"), ("retention", "retention")):
        result[prefix + "_total"] = len(groups[name])
        result[prefix + "_before"] = count(name, "before")
        result[prefix + "_after"] = count(name, "after")
    # 保持率只计算基线正确且对照后仍正确的同一题，不能用两组总正确数相除。
    result["retention_after"] = sum(s.get("before_judgment", {}).get("correct") is True
                                    and s.get("after_judgment", {}).get("correct") is True
                                    for s in groups["retention"])
    result["incomplete_types"] = [name for name, rows in groups.items() if any(s.get("errors") for s in rows)]
    return result


def calculate_metrics(assessment, revisions, stage="final"):
    if "samples" in assessment:
        assessment = {**assessment, **summarize(assessment["samples"])}
    names = ["风险样本减少率", "目标编辑成功率", "泛化测试成功率", "非目标能力保持率"]
    pairs = [(assessment["risk_before"] - assessment["risk_after"], assessment["risk_before"]),
             (assessment["target_after"], assessment["target_total"]),
             (assessment["general_after"], assessment["general_total"]),
             (assessment["retention_after"], assessment["retention_before"])]
    result = []
    for code, name, (numerator, denominator), group in zip(CODES, names, pairs,
                                                         ("risk", "target", "generalization", "retention")):
        revision = next((r for r in revisions if r["metricCode"] == code), {})
        threshold = next((t for t in revision.get("thresholds", []) if t["stage"] == stage), None)
        reason = ("missing_samples" if not denominator else "incomplete_predictions"
                  if group in assessment.get("incomplete_types", []) else "missing_threshold" if not threshold else None)
        value = round(numerator / denominator, 4) if not reason else None
        passed = value is not None and (value <= threshold["value"] if threshold.get("comparator") == "lte"
                                       else value >= threshold["value"])
        result.append({"metricCode": code, "name": name, "revisionId": revision.get("revisionId", code),
                       "value": value, "numerator": numerator, "denominator": denominator,
                       "unit": "ratio", "counts": [], "thresholdSnapshot": threshold,
                       "judgmentStatus": "inconclusive" if reason else "passed" if passed else "failed",
                       "reasonCode": reason, "evidenceRefs": [], "gap": round(value - threshold["value"], 12) if value is not None else None,
                       "formula": revision.get("formula", "分子 / 分母"), "formulaCode": revision.get("formulaCode", code)})
    return result


def apply_run(run):
    block = run.snapshot["modelComparison"]
    if digest(block) != run.snapshot.get("modelComparisonHash"):
        raise HTTPException(409, "模型对照记录指纹不一致")
    metrics = calculate_metrics(block, run.snapshot.get("metricRevisions", []), run.snapshot.get("targetStage", "final"))
    selected = {r["metricCode"] for r in run.snapshot.get("metricRevisions", [])}
    run.metric_results = [{**m, "evidenceRefs": [f"evidence-{run.run_id}-{m['metricCode']}"]}
                          for m in metrics if m["metricCode"] in selected]
    states = [m["judgmentStatus"] for m in run.metric_results]
    run.judgment_status = "inconclusive" if not states or "inconclusive" in states else "failed" if "failed" in states else "passed"
    run.integrity_state = "complete"
    run.algorithm_mode = "simulated_policy" if block["simulation"] else "real_model_comparison"


def evidence_entries(run):
    block = run.snapshot["modelComparison"]
    valid = digest(block) == run.snapshot.get("modelComparisonHash")
    entries = []
    for metric in run.metric_results or []:
        material = {"source": run.snapshot["resolvedRefs"][0], "calculation": metric,
                    "comparison": block, "capturedAt": run.finished_at.isoformat() if run.finished_at else None}
        expected = next((m for m in calculate_metrics(block, run.snapshot.get("metricRevisions", []),
                         run.snapshot.get("targetStage", "final")) if m["metricCode"] == metric["metricCode"]), None)
        correct = expected is not None and all(metric.get(k) == expected.get(k) for k in expected if k != "evidenceRefs")
        entries.append({"evidenceId": metric["evidenceRefs"][0], "runId": run.run_id, "kind": "calculation",
                        "sourceRef": material["source"], "sha256": digest(material),
                        "integrityState": "complete" if valid and correct else "corrupt", "missingFields": [],
                        "redactedFields": [], "capturedAt": material["capturedAt"], "allowedActions": [],
                        "mimeType": "application/json", "material": material})
    return entries


def list_sources(db, keyword=""):
    from app.models.tables import EvaluationRun
    sources = []
    for run in db.scalars(select(EvaluationRun).order_by(EvaluationRun.created_at.desc())):
        block = (run.snapshot or {}).get("modelComparison")
        if not block or not run.config.get("capabilityTaskId") or run.task_status != "succeeded":
            continue
        if keyword and keyword.casefold() not in (run.name + run.record_id).casefold():
            continue
        sources.append({"entityType": "model_comparison", "entityId": run.run_id,
                        "versionId": run.snapshot["modelComparisonHash"],
                        "name": f"{run.name} · {block['baseline']} → {block['edited']} · {run.record_id}",
                        "sourceModule": "model-training", "sourceTaskId": run.task_id,
                        "sourceTraceId": run.task_id, "taskTraceId": run.task_id,
                        "capabilityCode": "model_comparison", "status": "succeeded",
                        "algorithmMode": run.algorithm_mode, "datasetVersion": block["dataset_version"],
                        "labelVersion": VERSION, "modelVersion": block["baseline"] + " → " + block["edited"],
                        "sampleCount": len(block["samples"]), "canRerun": False,
                        "contentHash": run.snapshot["modelComparisonHash"], "capturedAt": run.created_at.isoformat(),
                        "evaluatorVersion": VERSION, "supportedMetricCodes": CODES,
                        "evaluationIssues": ["模拟治理策略对照；未执行模型权重编辑。"] if block["simulation"] else []})
    return sources


def resolve_source(db, ref):
    source = next((s for s in list_sources(db) if s["entityId"] == ref.get("entityId", ref.get("entity_id"))
                   and s["versionId"] == ref.get("versionId", ref.get("version_id"))), None)
    if not source:
        raise HTTPException(400, "对照来源不存在或版本发生变化")
    from app.models.tables import EvaluationRun
    run = db.get(EvaluationRun, source["entityId"])
    if digest(run.snapshot["modelComparison"]) != source["contentHash"]:
        raise HTTPException(409, "对照来源指纹不一致")
    return source


def create(db, body, token):
    own = owner(token)
    if body.baseline == body.edited:
        raise HTTPException(400, "基线与对照版本必须不同")
    for version in (body.baseline, body.edited):
        if not db.scalar(select(ModelVersion).where(ModelVersion.model_id == body.model_id,
                                                    ModelVersion.version == version)):
            raise HTTPException(400, "平台模型版本不存在")
    from urllib.parse import quote
    baseline_binding = llm(f"/model-evaluation/bindings/{body.model_id}/{quote(body.baseline, safe='')}", token)
    edited_binding = llm("/model-evaluation/prepare-edit", token,
                         {"platform_model_id": body.model_id, "version": body.edited})
    bindings = [baseline_binding, edited_binding]
    if bindings[0]["mode"] != "baseline" or bindings[1]["mode"] == "baseline":
        raise HTTPException(400, "请选择基线绑定和模拟治理/真实编辑绑定")
    if bindings[1]["mode"] == "simulated_policy" and bindings[0]["model_id"] != bindings[1]["model_id"]:
        raise HTTPException(400, "模拟治理对照必须使用同一个推理模型")
    dataset_name, version_id, questions = freeze_questions(db, body.dataset_id, body.dataset_version)
    config = {**body.model_dump(), "dataset_version_id": version_id, "questions": questions,
              "bindings": bindings, "owner": own, "version": VERSION,
              "input_hash": digest(questions), "temperature": 0, "max_tokens": 1024}
    now = now_shanghai()
    tid = "comparison_" + uuid4().hex[:20]
    task = Task(task_id=tid, name="模型前后真实对照评估", capability_code="evaluation",
                status="pending", progress=0, trace_id=tid, dataset_name=dataset_name,
                model_version=f"{body.baseline} → {body.edited}",
                input_data={"model_id": str(body.model_id), "dataset_id": str(body.dataset_id)},
                config=config, result={"comparison_version": VERSION, "processed": 0, "total": len(questions)},
                created_at=now)
    db.add(task)
    db.commit()
    return task


def execute(task_id, token):
    """独立于页面轮询执行；每题落库，重启后保留已完成明细并明确失败。"""
    try:
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            task.status = "running"
            db.commit()
            config = task.config
        rows = []
        for sample in config["questions"]:
            row = {**sample, "before": "", "after": "", "errors": {}, "call_ids": {}}
            for side, binding in zip(("before", "after"), config["bindings"]):
                try:
                    response = llm("/model-evaluation/answer", token,
                                   {"platform_model_id": config["model_id"], "version": binding["version"],
                                    "binding_hash": binding["binding_hash"], "question": sample["input"]})
                    output = response["output"]
                    if not isinstance(output, str) or not output.strip():
                        raise ValueError("模型返回空回答")
                    row[side] = output
                    row[side + "_judgment"] = judge(output, sample)
                    row["call_ids"][side] = response.get("call_ids", [])
                except Exception as exc:
                    row["errors"][side] = str(exc.detail) if isinstance(exc, HTTPException) else str(exc)
            rows.append(row)
            with SessionLocal() as db:
                task = db.get(Task, task_id)
                task.progress = round(100 * len(rows) / len(config["questions"]))
                task.result = {"comparison_version": VERSION, "processed": len(rows),
                               "total": len(config["questions"]), "samples": rows}
                db.commit()
        assessment = {**summarize(rows), "id": "assessment-" + task_id, "task_id": task_id,
                      "model_id": str(config["model_id"]), "dataset_id": str(config["dataset_id"]),
                      "baseline": config["baseline"], "edited": config["edited"],
                      "dataset_version": config["dataset_version"], "status": "succeeded",
                      "knowledge": "版本绑定对照", "comparison_version": VERSION,
                      "simulation": config["bindings"][1]["mode"] == "simulated_policy",
                      "input_hash": config["input_hash"], "bindings": config["bindings"],
                      "dataset_version_id": config["dataset_version_id"]}
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            task.status = "failed" if all(len(s["errors"]) == 2 for s in rows) else "succeeded"
            assessment["status"] = task.status
            task.finished_at = now_shanghai()
            task.result = {**task.result, "model_assessment": assessment}
            from app.routers.evaluations import bridge_model_assessment
            bridge_model_assessment(db, task, assessment)
            db.commit()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("模型对照评估执行失败：%s", task_id)
        with SessionLocal() as db:
            task = db.get(Task, task_id)
            if task:
                task.status = "failed"
                task.finished_at = now_shanghai()
                task.result = {**(task.result or {}), "error": "评估执行中断，请检查后端日志；已生成明细保留"}
                db.commit()


def recover(db):
    for task in db.scalars(select(Task).where(Task.capability_code == "evaluation",
                                            Task.status.in_(("pending", "running")))):
        if (task.result or {}).get("comparison_version") == VERSION:
            task.status = "failed"
            task.finished_at = now_shanghai()
            task.result = {**task.result, "error": "后端重启导致本次评估中断，已完成明细保留，请重新发起"}
    db.commit()
