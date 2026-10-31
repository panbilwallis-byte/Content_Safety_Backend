"""从已完成的三类治理任务冻结输入并计算真实指标，不调用模型。

沿用 riskEvaluation/riskEvidence 快照键以兼容已归档风险评估。
"""
import hashlib
import json
import os
from datetime import datetime, timezone, timedelta

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.core.time import now_shanghai
from app.models.tables import DatasetRecord, DatasetVersionItem, EvaluationMetric, EvaluationMetricRevision
from app.services.dataset_version_service import resolve_version, manifest_hash


METRICS = {
    "risk_level_accuracy": ("风险等级准确率", "accuracy", "gte", .95,
                            "四档等级完全一致数 / 全部有标准答案的样本数（失败、无法判断及提示不计为正确）"),
    "risk_high_recall": ("高风险检出率", "recall", "gte", .95,
                         "正确判为高风险数 / 人工标为高风险的全部样本数（含失败及无法判断）"),
    "risk_safe_fpr": ("安全样本误报率", "fpr", "lte", .05,
                      "安全样本中被列为风险候选数（含提示） / 人工标为安全的全部样本数；缺失判断时不作达标结论"),
}
METRICS.update({
    "value_tier_accuracy": ("价值档位准确率", "accuracy", "gte", .95, "价值档位完全一致数 / 全部有人工价值档位的样本数（失败及不可评估不计正确）"),
    "value_high_recall": ("高价值检出率", "recall", "gte", .95, "正确判为高价值数 / 人工标为高价值的全部样本数"),
    "anomaly_accuracy": ("异常识别准确率", "accuracy", "gte", .95, "正常或异常判断正确数 / 全部有人工异常标注的样本数（失败及不可评估不计正确）"),
    "anomaly_recall": ("异常检出率", "recall", "gte", .95, "正确检出的异常样本数 / 人工标为异常的全部样本数"),
    "anomaly_normal_fpr": ("正常样本误报率", "fpr", "lte", .05, "正常样本中被判为异常数 / 人工标为正常的全部样本数；缺失判断时不作达标结论"),
})
KINDS = {
    "governance-risk": ("风险识别与分级", "risk-classification", "reference_risk_level", "人工风险等级标注", tuple(c for c in METRICS if c.startswith("risk_"))),
    "governance-value": ("数据价值分析", "value-analysis", "reference_value_tier", "人工价值档位标注", ("value_tier_accuracy", "value_high_recall")),
    "governance-anomaly": ("异常数据治理", "anomaly", "reference_is_anomaly", "人工异常标注", ("anomaly_accuracy", "anomaly_recall", "anomaly_normal_fpr")),
}
GOVERNANCE_VERSION = "governance-reference-v1"
LEVELS = {"safe", "low", "medium", "high"}
VERSION = "risk-reference-v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def load_json(value):
    return json.loads(value) if isinstance(value, str) else value


def token_hash():
    token = os.getenv("LLM_ACCESS_TOKEN", "")
    if not token:
        raise HTTPException(503, "未配置业务后端 LLM_ACCESS_TOKEN，无法读取治理任务来源")
    return hashlib.sha256(token.encode()).hexdigest()


def source_rows(db, result_id=None):
    owner = token_hash()
    try:
        authorization = db.execute(text("SELECT enabled,expires_at,deleted_at FROM llm_auth_tokens WHERE token_hash=:token"),
                                   {"token": owner}).mappings().first()
    except SQLAlchemyError as exc:
        raise HTTPException(503, "模型服务令牌记录暂不可用") from exc
    if not authorization or not authorization["enabled"] or authorization["deleted_at"]:
        raise HTTPException(403, "业务后端配置的模型服务令牌已停用或不存在")
    expires = datetime.fromisoformat(authorization["expires_at"])
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise HTTPException(403, "业务后端配置的模型服务令牌已过期")
    sql = """SELECT r.id, r.task_id, r.kind, r.summary_json, r.created_at,
                    t.scope_json, t.model_id, t.model_version, m.name AS model_name
             FROM llm_governance_results r JOIN llm_governance_tasks t ON t.id=r.task_id
             LEFT JOIN llm_models m ON m.id=t.model_id
             WHERE r.kind IN ('governance-risk','governance-value','governance-anomaly') AND t.status='succeeded'
               AND r.token_hash=:token AND t.token_hash=:token"""
    params = {"token": owner}
    if result_id:
        sql += " AND r.id=:id"
        params["id"] = result_id
    sql += " ORDER BY r.created_at DESC, r.id DESC"
    try:
        return list(db.execute(text(sql), params).mappings())
    except SQLAlchemyError as exc:
        raise HTTPException(503, "治理任务来源暂不可用，请检查共享数据库中的模型服务表") from exc


def source_context(row):
    summary = load_json(row["summary_json"])
    scope = load_json(row["scope_json"])
    result_id = row["id"]
    stamp = datetime.fromisoformat(summary.get("finished_at") or row["created_at"])
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    finished = stamp.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    version = summary.get("version_label") or scope["version_id"]
    kind = row["kind"]
    title, module, field, label_name, metrics = KINDS[kind]
    issues = []
    if summary.get("data_source") == "mock":
        issues.append("该结果使用模拟样本，不能用于真实评估。请接入真实记录后重新分析。")
    if kind != "governance-risk" and summary.get("evaluation_contract") != GOVERNANCE_VERSION:
        issues.append("历史结果未冻结人工标准答案或未隔离标准标签，请补充标注后重新执行分析。")
    return {
        "entityType": "task_result", "entityId": result_id, "versionId": result_id,
        "name": f"【{title}】{summary.get('dataset_name', title)} {version}｜{row['model_name'] or row['model_version']}｜{finished}｜{row['task_id'][:8]}",
        "sourceModule": module, "sourceTaskId": row["task_id"],
        "sourceTraceId": row["task_id"], "taskTraceId": row["task_id"],
        "capabilityCode": kind, "status": "succeeded", "algorithmMode": "mock" if summary.get("data_source") == "mock" else "llm",
        "datasetId": str(scope["dataset_id"]), "modelId": row["model_id"],
        "datasetVersion": version, "labelVersion": f"{scope['version_id']} {label_name}",
        "modelVersion": row["model_version"], "evaluatorVersion": VERSION,
        "contentHash": digest({"id": result_id, "summary": summary}),
        "sampleCount": sum(int(summary.get(k, 0)) for k in ("valid_count", "unassessable_count", "unavailable_count", "failed_count")),
        "canRerun": False, "capturedAt": summary.get("finished_at") or row["created_at"],
        "interface": f"已完成{title}结果（只读引用）", "supportedMetricCodes": list(metrics),
        "evaluationIssues": issues, "referenceField": field,
    }


def list_sources(db, keyword=""):
    candidates = [source_context(row) for row in source_rows(db)]
    return [s for s in candidates if not keyword or keyword.casefold() in (s["name"] + s["sourceTaskId"]).casefold()]


def resolve_source(db, ref):
    key = ref.get("entityId", ref.get("entity_id"))
    version = ref.get("versionId", ref.get("version_id"))
    rows = source_rows(db, key)
    if not rows or key != version:
        raise HTTPException(400, "治理结果不存在、无访问权限或版本不匹配")
    return source_context(rows[0])


def freeze_source(db, source):
    """从原始任务检查点重建全部样本，包含失败与无法判断，拒绝标准答案漂移。"""
    row = source_rows(db, source["entityId"])[0]
    scope = load_json(row["scope_json"])
    summary = load_json(row["summary_json"])
    if source.get("evaluationIssues"):
        raise HTTPException(400, "；".join(source["evaluationIssues"]))
    if row["kind"] != "governance-risk":
        return freeze_governance(db, row, scope, summary)
    version = resolve_version(db, int(scope["dataset_id"]), scope["version_id"])
    if version is None:
        raise HTTPException(400, "来源任务的数据版本清单已不存在")
    records = db.execute(select(DatasetRecord, DatasetVersionItem.content_hash).join(
        DatasetVersionItem, DatasetVersionItem.record_id == DatasetRecord.id
    ).where(DatasetVersionItem.version_id == version.version_id).order_by(DatasetRecord.id)).all()
    if len(records) != version.sample_count or len(records) != source["sampleCount"]:
        raise HTTPException(409, "风险任务样本数量与固定清单不一致")
    if any(record.dataset_id != int(scope["dataset_id"]) or record.content_hash != hashed for record, hashed in records):
        raise HTTPException(409, "来源清单与原始记录指纹不一致")
    if (manifest_hash((r.id, hashed) for r, hashed in records) != version.manifest_hash or
            summary.get("manifest_hash") != version.manifest_hash):
        raise HTTPException(409, "风险任务与当前数据清单指纹不一致，请重新检测")
    outputs = db.execute(text("SELECT sample_id,status,output_json,error_code FROM llm_governance_task_samples WHERE task_id=:id ORDER BY sample_id"),
                         {"id": row["task_id"]}).mappings().all()
    by_id = {r["sample_id"]: r for r in outputs}
    if set(by_id) != {str(r.id) for r, _ in records}:
        raise HTTPException(409, "风险任务检查点与固定清单的样本不一致")
    public = db.execute(text("SELECT samples_json FROM llm_governance_results WHERE id=:id AND token_hash=:token"),
                        {"id": row["id"], "token": token_hash()}).scalar_one()
    saved = {r["id"]: r for r in load_json(public)}
    samples = []
    for record, _ in records:
        ref = (record.payload or {}).get("reference_risk_level")
        if ref is not None and ref not in LEVELS:
            raise HTTPException(400, f"样本 {record.id} 的标准风险档位无效")
        sid = str(record.id)
        if sid in saved and saved[sid].get("reference_risk_level") != ref:
            raise HTTPException(409, f"样本 {sid} 的标准答案已变化，请重新检测")
        checkpoint = by_id[sid]
        output = load_json(checkpoint["output_json"]) or {}
        prediction = None
        status = "failed"
        reason = output.get("error_detail") or checkpoint["error_code"] or "未返回有效判断"
        if checkpoint["status"] == "success":
            if output.get("status") == "unassessable":
                status, reason = "unassessable", output.get("unassessableReason") or "无法判断"
            elif output.get("status") == "assessed":
                levels = [f.get("suggestedLevel") for f in output.get("findings", [])]
                if any(level not in {"HIGH", "MEDIUM", "LOW", "NOTICE"} for level in levels):
                    raise HTTPException(400, f"样本 {sid} 的模型风险档位无效")
                prediction = next((s.lower() for s in ("HIGH", "MEDIUM", "LOW", "NOTICE") if s in levels), "safe")
                status, reason = ("notice", "仅提示，未给出四档等级") if prediction == "notice" else ("assessed", "")
        correct = prediction == ref if ref is not None and prediction in LEVELS else None
        samples.append({"sampleId": sid, "expectedLevel": ref, "predictedLevel": prediction,
                        "predictionStatus": status, "correct": correct, "reason": reason,
                        "expected": None, "predicted": None, "baseline": None, "required": ref is not None})
    if not any(s["required"] for s in samples):
        raise HTTPException(400, "来源没有人工标准答案，无法计算风险准确率")
    block = {"version": VERSION, "sourceResultId": row["id"], "sourceTaskId": row["task_id"],
             "manifestHash": version.manifest_hash, "samples": samples,
             "sourceHash": digest({"summary": summary, "samples": load_json(public),
                                    "checkpoints": [dict(r) for r in outputs]})}
    return block


def freeze_governance(db, row, scope, summary):
    """价值/异常只接受已隔离标准答案的新任务；冻结检测结果，不把修复草稿当成正确答案。"""
    kind = row["kind"]
    version = resolve_version(db, int(scope["dataset_id"]), scope["version_id"])
    if version is None:
        raise HTTPException(400, "来源数据的固定版本清单不存在，请重新分析")
    records = db.execute(select(DatasetRecord, DatasetVersionItem.content_hash).join(
        DatasetVersionItem, DatasetVersionItem.record_id == DatasetRecord.id
    ).where(DatasetVersionItem.version_id == version.version_id).order_by(DatasetRecord.id)).all()
    if (len(records) != version.sample_count or
            any(r.dataset_id != int(scope["dataset_id"]) or r.content_hash != h for r, h in records) or
            manifest_hash((r.id, h) for r, h in records) != version.manifest_hash or
            summary.get("manifest_hash") != version.manifest_hash):
        raise HTTPException(409, "来源结果与固定数据清单不一致，请重新分析")
    language = scope.get("language", "all")
    if language != "all":
        records = [(r, h) for r, h in records if ((r.payload or {}).get("language") or (r.payload or {}).get("lang") or "unknown") == language]
    if any((r.payload or {}).get("is_mock") for r, _ in records):
        raise HTTPException(400, "模拟样本不能用于真实评估")
    outputs = db.execute(text("SELECT sample_id,status,output_json,error_code FROM llm_governance_task_samples WHERE task_id=:id ORDER BY sample_id"),
                         {"id": row["task_id"]}).mappings().all()
    by_id = {r["sample_id"]: r for r in outputs}
    if set(by_id) != {str(r.id) for r, _ in records}:
        raise HTTPException(409, "任务逐条结果与固定清单的样本不一致")
    field = KINDS[kind][2]
    annotations = summary.get("reference_annotations")
    if not isinstance(annotations, dict) or set(annotations) != set(by_id):
        raise HTTPException(409, "来源任务未完整冻结人工标注，请重新分析")
    public = load_json(db.execute(text("SELECT samples_json FROM llm_governance_results WHERE id=:id AND token_hash=:token"),
                                 {"id": row["id"], "token": token_hash()}).scalar_one())
    saved = {s["id"]: s for s in public}
    samples = []
    for record, _ in records:
        sid = str(record.id)
        ref = (record.payload or {}).get(field)
        if ref != annotations[sid] or type(ref) is not type(annotations[sid]):
            raise HTTPException(409, f"样本 {sid} 的人工标注已变化，请重新分析")
        if ref is not None and ((not isinstance(ref, str) or ref not in {"high", "medium", "low"}) if kind == "governance-value" else type(ref) is not bool):
            raise HTTPException(400, f"样本 {sid} 的 {field} 标注无效")
        expected = ref if kind == "governance-value" or ref is None else ("anomaly" if ref else "normal")
        checkpoint = by_id[sid]
        output = load_json(checkpoint["output_json"]) or {}
        predicted, status = None, "failed"
        reason = output.get("error_detail") or checkpoint["error_code"] or "未得到有效判断"
        score = None
        if checkpoint["status"] == "success":
            unavailable = output.get("status") == "unavailable"
            if unavailable:
                status, reason = "unassessable", output.get("unavailable_reason") or output.get("unavailableReason") or "不可评估"
            elif kind == "governance-value" and output.get("status") == "scored":
                policy = summary.get("evaluation_policy") or {}
                try:
                    values = {d["name"]: d["score"] for d in output["dimensions"]}
                    import math
                    if not policy["dimensions"] or any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 100 for v in values.values()):
                        raise ValueError("invalid scores")
                    score = round(sum(values[d["name"]] * d["weight"] for d in policy["dimensions"]), 1)
                    predicted = "high" if score >= policy["high_threshold"] else "medium" if score >= policy["medium_threshold"] else "low"
                except (KeyError, TypeError, ValueError) as exc:
                    raise HTTPException(409, "价值结果缺少有效的冻结评分方案或维度分数") from exc
                if sid not in saved or saved[sid].get("score") != score or saved[sid].get("tier") != predicted:
                    raise HTTPException(409, "价值评分与逐条检查点不一致")
                status, reason = "assessed", ""
            elif kind == "governance-anomaly" and output.get("status") == "assessed" and isinstance(output.get("findings"), list):
                predicted = "anomaly" if output["findings"] else "normal"
                status, reason = "assessed", ""
        display = {"high": "高价值", "medium": "中价值", "low": "低价值"} if kind == "governance-value" else {"anomaly": "异常", "normal": "正常"}
        samples.append({"sampleId": sid, "expectedLevel": expected, "predictedLevel": predicted,
                        "expectedDisplay": display.get(expected, "无标准答案"), "predictedDisplay": display.get(predicted, "无法判断" if status == "unassessable" else "检测失败"),
                        "predictionStatus": status, "correct": predicted == expected if expected is not None and predicted is not None else None,
                        "reason": reason, "expected": None, "predicted": None, "baseline": None,
                        "required": ref is not None, "score": score, "judgmentType": kind})
    if not any(s["required"] for s in samples):
        allowed = "high / medium / low" if kind == "governance-value" else "true（异常）/ false（正常），必须为布尔值"
        raise HTTPException(400, f"缺少人工标准答案：请在数据记录中补充 {field}（{allowed}），创建固定版本并重新分析")
    return {"version": GOVERNANCE_VERSION, "capabilityCode": kind, "sourceResultId": row["id"], "sourceTaskId": row["task_id"],
            "manifestHash": version.manifest_hash, "samples": samples,
            "sourceHash": digest({"summary": summary, "samples": public, "checkpoints": [dict(r) for r in outputs]})}


def metric_includes(sample, code):
    if not sample["required"]:
        return False
    if code in {"risk_high_recall", "value_high_recall"}:
        return sample["expectedLevel"] == "high"
    if code == "risk_safe_fpr":
        return sample["expectedLevel"] == "safe"
    if code == "anomaly_recall":
        return sample["expectedLevel"] == "anomaly"
    if code == "anomaly_normal_fpr":
        return sample["expectedLevel"] == "normal"
    return True


def metric_positive(sample, code):
    if code in {"risk_level_accuracy", "value_tier_accuracy", "anomaly_accuracy"}:
        return sample["correct"] is True
    if code in {"risk_high_recall", "value_high_recall"}:
        return sample["predictedLevel"] == "high"
    if code == "risk_safe_fpr":
        return sample["predictedLevel"] in {"low", "medium", "high", "notice"}
    return sample["predictedLevel"] == "anomaly"


def metric_failed(sample, code):
    positive = metric_positive(sample, code)
    return (positive or sample["predictedLevel"] is None) if METRICS[code][1] == "fpr" else not positive


def ensure_metrics(db):
    """补登三类治理指标，默认阈值可在指标管理中修订。"""
    from uuid import uuid4
    now = now_shanghai()
    for code, (name, formula, comparator, target, description) in METRICS.items():
        if db.scalar(select(EvaluationMetric).where(EvaluationMetric.code == code)):
            continue
        mid = f"metric-{uuid4().hex[:16]}"
        category = "data_value" if code.startswith("value_") else "data_governance" if code.startswith("anomaly_") else "risk_detect"
        definition = {"formulaCode": formula, "formulaVersion": VERSION, "formula": description,
                      "denominatorDefinition": description.split(" / ", 1)[-1], "positiveClass": "高价值" if code.startswith("value_") else "异常" if code.startswith("anomaly_") else "高风险",
                      "unit": "ratio", "testMethod": "automatic", "applicableObjects": "已完成且有标准答案的对应治理任务",
                      "parameters": [], "thresholds": [{"stage": stage, "comparator": comparator, "value": target, "unit": "ratio"}
                                                      for stage in ("midterm", "final")],
                      "inputRequirements": ["固定清单", "人工标准答案", "逐条模型判断"],
                      "requiredEvidence": ["逐条对错明细"], "sourceDocumentRefs": []}
        db.add(EvaluationMetric(metric_id=mid, code=code, name=name, category=category, status="enabled",
                                description="引用真实治理任务计算。默认阈值仅供配置起步，请按项目要求确认。",
                                configuration_status="published", issues=[]))
        db.add(EvaluationMetricRevision(revision_id=f"revision-{uuid4().hex[:16]}", metric_id=mid,
                                       metric_code=code, name=name, category=category, revision_no=1,
                                       configuration_status="published", definition=definition, published_at=now))


def calculate(run):
    snapshot = dict(run.snapshot or {})
    block = snapshot.get("riskEvaluation")
    if not block or digest(block) != snapshot.get("riskEvaluationHash"):
        raise HTTPException(409, "冻结的治理评估输入不完整或指纹不一致")
    samples = block["samples"]
    kind = block.get("capabilityCode", "governance-risk")
    results = []
    for index, revision in enumerate(snapshot["metricRevisions"]):
        code = revision["metricCode"]
        if code not in KINDS[kind][4]:
            raise HTTPException(400, "此来源包含不支持的指标")
        relevant = [s for s in samples if metric_includes(s, code)]
        numerator = sum(metric_positive(s, code) for s in relevant)
        denominator = len(relevant)
        # 固定精度，避免 MySQL JSON 对长小数末位的规范化改变证据摘要。
        value = round(numerator / denominator, 12) if denominator else None
        threshold = next((x for x in revision.get("thresholds", []) if x["stage"] == snapshot["targetStage"]), None)
        reason = "zero_denominator" if not denominator else ("stage_target_missing" if not threshold else None)
        if code in {"risk_safe_fpr", "anomaly_normal_fpr"} and any(s["predictionStatus"] in {"failed", "unassessable"} for s in relevant):
            reason = "predictions_missing"
        passed = False
        if value is not None and threshold:
            comparator, target = threshold["comparator"], threshold["value"]
            passed = {"gte": value >= target, "lte": value <= target, "eq": value == target}.get(comparator, False)
        results.append({"metricCode": code, "name": revision["name"], "revisionId": revision["revisionId"],
                        "value": value, "unit": "ratio", "numerator": numerator, "denominator": denominator,
                        "counts": [{"name": "全部样本", "value": len(samples)},
                                   {"name": "调用或校验失败", "value": sum(s["predictionStatus"] == "failed" for s in samples)},
                                   {"name": "无法判断", "value": sum(s["predictionStatus"] == "unassessable" for s in samples)},
                                   {"name": "未标注", "value": sum(not s["required"] for s in samples)}],
                        "thresholdSnapshot": threshold, "judgmentStatus": "inconclusive" if reason else ("passed" if passed else "failed"),
                        "reasonCode": reason, "evidenceRefs": [f"evidence-{run.run_id}-{index}"],
                        "gap": round(value - threshold["value"], 12) if value is not None and threshold else None,
                        "formula": METRICS[code][4], "formulaCode": METRICS[code][1]})
    run.metric_results = results
    run.algorithm_mode = {"governance-risk": "risk_reference", "governance-value": "value_reference", "governance-anomaly": "anomaly_reference"}[kind]
    run.judgment_status = "failed" if any(r["judgmentStatus"] == "failed" for r in results) else (
        "inconclusive" if any(r["judgmentStatus"] == "inconclusive" for r in results) else "passed")
    run.integrity_state = "complete"
    evidence = []
    for result in results:
        material = {"source": snapshot["resolvedRefs"][0], "calculation": result,
                    "capturedAt": run.finished_at.isoformat(), "samples": samples,
                    "inputHash": snapshot["riskEvaluationHash"]}
        evidence.append({"evidenceId": result["evidenceRefs"][0], "runId": run.run_id, "kind": "calculation",
                         "sourceRef": material["source"], "sha256": digest(material), "integrityState": "complete",
                         "missingFields": [], "redactedFields": ["样本正文"], "capturedAt": material["capturedAt"],
                         "allowedActions": [], "mimeType": "application/json", "material": material})
    snapshot["riskEvidence"] = evidence
    run.snapshot = snapshot


def evidence_entries(run):
    snapshot = run.snapshot or {}
    input_valid = bool(snapshot.get("riskEvaluation")) and digest(snapshot.get("riskEvaluation")) == snapshot.get("riskEvaluationHash")
    entries = []
    for stored in snapshot.get("riskEvidence", []):
        entry = dict(stored)
        material = entry["material"]
        valid = (input_valid and digest(material) == entry.get("sha256") and
                 material.get("inputHash") == snapshot.get("riskEvaluationHash") and
                 material.get("samples") == snapshot["riskEvaluation"]["samples"] and
                 material.get("source") == snapshot["resolvedRefs"][0] and
                 material.get("calculation") in (run.metric_results or []))
        entry["integrityState"] = "complete" if valid else "corrupt"
        entries.append(entry)
    return entries
