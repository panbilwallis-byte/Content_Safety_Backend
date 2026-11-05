"""合规材料的只追加快照及证据计算；查询来源不产生记录。"""
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4
from urllib.parse import quote
import os
import re

import httpx
from fastapi import HTTPException
from sqlalchemy import select

from app.core.time import now_shanghai
from app.models.tables import (EvaluationWorkspaceItem, EvaluationMetric, EvaluationMetricRevision,
                               Task, TrainingTask, Dataset, Model, ModelVersion, ComplianceEvidence)
from app.services.risk_evaluation_service import digest

VERSION = "audit-reference-v1"
TITLES = {"lineage": "数据谱系", "task": "任务五要素", "checkpoints": "训练检查点"}
CODES = {"lineage": "audit_lineage_coverage", "task": "audit_task_completeness",
         "checkpoints": "audit_checkpoint_coverage"}
RULES = {"lineage": "data-training-output-v1", "task": "input-time-interface-version-output-v1",
         "checkpoints": "interval-and-final-epoch-v1"}


def check(sid, name, state, reason, evidence=None):
    return {"sampleId": sid, "expected": True, "predicted": state == "verified" if state != "unknown" else None,
            "expectedLevel": "required", "expectedDisplay": name + "：应有可核验材料",
            "predictedDisplay": {"verified": "已核验", "missing": "缺失", "unknown": "无法核验"}[state],
            "correct": state == "verified" if state != "unknown" else None,
            "judgmentType": "audit", "predictionStatus": state, "required": True,
            "reason": reason, "evidenceRefs": evidence or []}


def _not_simulated(row):
    metadata = getattr(row, "metadata_json", None) or {}
    config = getattr(row, "config", None) or {}
    result = getattr(row, "result", None) or {}
    if metadata.get("is_mock") or config.get("simulation") or result.get("simulation") or result.get("data_source") == "mock" or result.get("is_mock"):
        raise HTTPException(400, "模拟对象不能登记为真实审计来源")


def _evidence(db, refs):
    if not refs:
        return "unknown"
    for ref in refs:
        row = db.get(ComplianceEvidence, ref)
        if not row:
            return "unknown"
        payload = row.payload or {}
        if payload.get("is_mock") or payload.get("algorithm_mode") == "mock" or "fixture" in str(payload.get("adapter_version", "")):
            return "unknown"
        if payload.get("integrity_state") != "verified":
            return "missing"
    return "verified"


def task_material(db, sid):
    from app.services.compliance_service import process_evidence, task_evidence
    row = db.get(Task, sid)
    if not row:
        raise HTTPException(404, "任务不存在")
    _not_simulated(row)
    if row.status != "succeeded":
        raise HTTPException(400, "请先选择已完成任务")
    payload = (process_evidence if row.capability_code == "data_process" else task_evidence)(row, "audit-task-" + sid)
    fields = {f["key"]: f for f in payload.get("redacted_fields", [])}
    rows = []
    for key, label in zip(("input", "time", "interface", "version", "output"), ("输入", "时间", "接口", "版本", "输出")):
        field = fields.get(key)
        state = "verified" if field and field.get("state") == "verified" and field.get("value") is not None else "missing"
        rows.append(check(key, label, state, "依据任务持久化字段检查；不代表业务执行正确", [payload["id"]]))
    return row.name, row.dataset_version or "", row.model_version, rows, {"fields": list(fields.values()), "taskId": sid}


def lineage_material(db, kind, sid, version, direction):
    from app.routers.compliance import _graph, _version_labels
    if kind not in {"dataset", "model"} or not sid.isdigit():
        raise HTTPException(422, "请选择数据集或模型")
    subject = db.get(Dataset if kind == "dataset" else Model, int(sid))
    if not subject:
        raise HTTPException(404, "谱系对象不存在")
    _not_simulated(subject)
    if not version:
        raise HTTPException(422, "保存谱系快照必须选择精确版本")
    wanted = _version_labels(db).get((sid, version), version) if kind == "dataset" else version
    nodes, edges = _graph(db)
    selected = {n["id"] for n in nodes if n["entity_type"] == kind and str(n["entity_id"]) == sid and n["version_id"] == wanted}
    if not selected:
        raise HTTPException(404, "对象的指定版本未登记")
    while True:
        previous = set(selected)
        for edge in edges:
            if ((direction != "downstream" and edge["to_id"] in selected) or
                    (direction != "upstream" and edge["from_id"] in selected)):
                selected.update((edge["from_id"], edge["to_id"]))
        if selected == previous:
            break
    nodes = [n for n in nodes if n["id"] in selected]
    edges = [e for e in edges if e["from_id"] in selected and e["to_id"] in selected]
    def registered(n):
        if n["entity_type"] == "training_task":
            task = db.get(TrainingTask, str(n["entity_id"]))
            return task is not None and len(task.id) >= 20 and task.status == "succeeded"
        if not str(n["entity_id"]).isdigit():
            return False
        if n["entity_type"] == "dataset":
            from app.services.dataset_version_service import resolve_version
            return resolve_version(db, int(n["entity_id"]), n["version_id"]) is not None
        if n["entity_type"] == "model":
            version_row = db.scalar(select(ModelVersion).where(ModelVersion.model_id == int(n["entity_id"]), ModelVersion.version == n["version_id"]))
            producing = db.get(TrainingTask, version_row.task_id) if version_row and version_row.task_id else None
            return producing is not None and len(producing.id) >= 20 and producing.status == "succeeded"
        return False
    rows = [check(key, label, "verified" if any(n["entity_type"] == node_kind and registered(n) for n in nodes) else "missing",
                  "按独立三阶段规则核查已登记节点；不评价标准链路还原准确率")
            for key, label, node_kind in (("data", "数据阶段", "dataset"), ("training", "训练阶段", "training_task"), ("output", "模型产物阶段", "model"))]
    for edge in edges:
        state = "missing" if edge["verification_state"] == "missing" else _evidence(db, edge["evidence_refs"])
        training_id = edge.get("training_task_id")
        if training_id and len(training_id) < 20:
            state = "unknown"
        rows.append(check(edge["id"], "关联 " + edge["relation"], state,
                          edge.get("missing_reason") or "核查关联记录与已登记证据；未核验实际训练消费数据", edge["evidence_refs"]))
    return subject.name, wanted if kind == "dataset" else "", wanted if kind == "model" else None, rows, {"nodes": nodes, "edges": edges}


def training_artifacts(task_id):
    """核对 8001 已登记权重的真实字节；不可读取时由调用者明确记录为 unknown。"""
    from app.routers.training_tasks import LLM_TRAINING_BASE_URL
    token = os.getenv("LLM_ACCESS_TOKEN", "")
    if not token:
        raise ValueError("未配置模型服务令牌")
    base = f"{LLM_TRAINING_BASE_URL}/tasks/{quote(task_id, safe='')}/artifacts"
    result = []
    with httpx.Client(timeout=30, headers={"Authorization": f"Bearer {token}"}) as client:
        response = client.get(base)
        response.raise_for_status()
        envelope = response.json()
        if envelope.get("code") != 0:
            raise ValueError("产物目录查询失败")
        entries = envelope.get("data") or []
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ValueError("产物目录格式无效")
        for entry in entries:
            if not str(entry.get("filename", "")).endswith((".pt", ".pth", ".safetensors")):
                continue
            item = {k: entry.get(k) for k in ("id", "filename", "epoch", "sha256", "size")}
            expected = str(entry.get("sha256") or "")
            item["verified"] = False
            if re.fullmatch(r"[a-f0-9]{64}", expected) and 0 < int(entry.get("size") or 0) <= 512 * 1024 * 1024:
                hasher, size = sha256(), 0
                with client.stream("GET", base + "/" + quote(str(entry['id']), safe='') + "/download") as download:
                    download.raise_for_status()
                    for chunk in download.iter_bytes():
                        size += len(chunk)
                        if size > 512 * 1024 * 1024:
                            raise ValueError("单个产物超过快照核验大小限制")
                        hasher.update(chunk)
                item["verified"] = hasher.hexdigest() == expected and size == entry["size"]
            result.append(item)
    return result


def checkpoint_material(db, sid):
    task = db.get(TrainingTask, sid)
    if not task:
        raise HTTPException(404, "训练任务不存在")
    if len(task.id) < 20:
        raise HTTPException(400, "历史演示训练不能作为真实检查点来源")
    if task.status != "succeeded" or task.epochs < 1:
        raise HTTPException(400, "请选择已完成真实训练")
    interval = max(1, task.epochs // 3)
    expected = [e for e in range(1, task.epochs + 1) if e % interval == 0 or e == task.epochs]
    saved = {item.get("epoch"): item for item in task.checkpoints or []}
    try:
        artifacts, issue = training_artifacts(sid), ""
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        artifacts, issue = [], "模型产物不可读取，未完成真实文件核验"
    rows = []
    for epoch in expected:
        matches = [a for a in artifacts if a.get("epoch") == epoch or
                   (str(a.get("filename", "")).rsplit("/", 1)[-1] == "last.pt" and epoch == task.epochs)]
        state = "missing" if epoch not in saved else ("verified" if any(a["verified"] for a in matches) else "unknown")
        rows.append(check(str(epoch), f"第 {epoch} 轮检查点", state,
                          issue or ("缺少应存检查点" if state == "missing" else "只认可明确对应轮次且文件摘要一致的产物"),
                          [str(a["id"]) for a in matches]))
    return task.name, task.dataset_version, task.target_version, rows, {"taskId": sid, "interval": interval,
                "epochs": task.epochs, "expectedEpochs": expected, "checkpoints": task.checkpoints, "artifacts": artifacts}


def create_snapshot(db, body, actor):
    kind = body.kind
    if kind == "lineage":
        values = lineage_material(db, body.entity_type, body.entity_id, body.version_id, body.direction)
    elif kind == "task":
        values = task_material(db, body.entity_id)
    else:
        values = checkpoint_material(db, body.entity_id)
    name, data_version, model_version, samples, material = values
    sid = "audit_" + uuid4().hex
    block = {"version": VERSION, "kind": kind, "ruleVersion": RULES[kind], "samples": samples,
             "material": material, "subjectId": body.entity_id, "subjectType": body.entity_type,
             "subjectVersion": body.version_id, "direction": body.direction, "actor": actor,
             "capturedAt": now_shanghai().isoformat(), "name": name,
             "datasetVersion": data_version, "modelVersion": model_version}
    row = EvaluationWorkspaceItem(item_id=sid, item_type="audit_snapshot", payload={"block": block, "hash": digest(block)})
    db.add(row)
    ensure_metrics(db)
    db.flush()
    return context(row)


def context(row):
    p = row.payload or {}
    block = p.get("block", {})
    if not block or digest(block) != p.get("hash") or block.get("version") != VERSION:
        raise HTTPException(409, "审计快照内容或摘要损坏")
    return {"entityType": "audit_record", "entityId": row.item_id, "versionId": p["hash"],
            "name": f"【{TITLES[block['kind']]}】{block['name']}｜{block.get('modelVersion') or block.get('datasetVersion') or block['subjectId']}｜{block['capturedAt']}｜{row.item_id[:14]}",
            "sourceModule": "compliance", "sourceTaskId": block["subjectId"], "sourceTraceId": row.item_id,
            "taskTraceId": row.item_id, "capabilityCode": "audit_" + block["kind"], "status": "succeeded",
            "algorithmMode": "real", "datasetId": "", "modelId": None,
            "datasetVersion": block["datasetVersion"], "labelVersion": block["ruleVersion"],
            "modelVersion": block["modelVersion"], "evaluatorVersion": VERSION, "contentHash": p["hash"],
            "sampleCount": len(block["samples"]), "canRerun": False, "capturedAt": block["capturedAt"],
            "subjectType": block["subjectType"], "subjectVersion": block["subjectVersion"], "direction": block["direction"],
            "interface": "合规审计固定快照", "supportedMetricCodes": [CODES[block["kind"]]], "evaluationIssues": []}


def list_sources(db, keyword=""):
    rows = db.scalars(select(EvaluationWorkspaceItem).where(EvaluationWorkspaceItem.item_type == "audit_snapshot").order_by(EvaluationWorkspaceItem.created_at.desc())).all()
    results = []
    for row in rows:
        try:
            source = context(row)
        except HTTPException:
            continue
        if not keyword or keyword.casefold() in (source["name"] + source["entityId"]).casefold():
            results.append(source)
    return results


def freeze(db, ref):
    row = db.get(EvaluationWorkspaceItem, ref.get("entityId", ref.get("entity_id")))
    if not row or row.item_type != "audit_snapshot":
        raise HTTPException(404, "审计快照不存在或不可访问")
    source = context(row)
    if ref.get("versionId", ref.get("version_id")) != source["versionId"]:
        raise HTTPException(409, "审计快照版本不一致")
    return deepcopy(row.payload["block"])


def resolve_source(db, ref):
    freeze(db, ref)
    return context(db.get(EvaluationWorkspaceItem, ref.get("entityId", ref.get("entity_id"))))


def ensure_metrics(db):
    for kind, code in CODES.items():
        if db.scalar(select(EvaluationMetric).where(EvaluationMetric.code == code)):
            continue
        mid = "metric-" + uuid4().hex[:16]
        name = TITLES[kind] + "留痕核验率"
        definition = {"formulaCode": "coverage", "formulaVersion": VERSION, "formula": "已核验检查项 / 全部应检查项", "unit": "ratio",
                      "numeratorDefinition": "具有可核验材料且满足冻结规则的检查项", "denominatorDefinition": "冻结规则要求的全部检查项，包含缺失和无法核验",
                      "positiveClass": "留痕核验通过", "applicableObjects": TITLES[kind], "parameters": [], "sourceDocumentRefs": [],
                      "category": "compliance", "testMethod": "evidence", "inputRequirements": ["固定审计快照"],
                      "requiredEvidence": ["逐项检查依据"], "thresholds": [{"stage": stage, "comparator": "gte", "value": 1, "unit": "ratio"} for stage in ("midterm", "final")]}
        db.add(EvaluationMetric(metric_id=mid, code=code, name=name, category="compliance", status="enabled", description="真实留痕核查，不证明业务正确或完整标准链路还原", configuration_status="published", issues=[]))
        db.add(EvaluationMetricRevision(revision_id="revision-" + uuid4().hex[:16], metric_id=mid, metric_code=code,
               name=name, category="compliance", revision_no=1, configuration_status="published", definition=definition, published_at=now_shanghai()))


def calculate(run):
    snapshot = dict(run.snapshot or {})
    block = snapshot.get("auditEvaluation")
    if not block or digest(block) != snapshot.get("auditEvaluationHash"):
        raise HTTPException(409, "冻结审计输入指纹不一致")
    samples = block["samples"]
    results = []
    for index, revision in enumerate(snapshot["metricRevisions"]):
        if revision["metricCode"] != CODES[block["kind"]]:
            raise HTTPException(400, "指标不适用于此审计来源")
        numerator = sum(s["predictionStatus"] == "verified" for s in samples)
        value = round(numerator / len(samples), 12) if samples else None
        target = next((t for t in revision.get("thresholds", []) if t["stage"] == snapshot["targetStage"]), None)
        unknown = any(s["predictionStatus"] == "unknown" for s in samples)
        reason = "zero_denominator" if not samples else "predictions_missing" if unknown else "stage_target_missing" if not target else None
        passed = value is not None and target and {"gte": value >= target["value"], "lte": value <= target["value"], "eq": value == target["value"]}.get(target["comparator"], False)
        results.append({"metricCode": revision["metricCode"], "name": revision["name"], "revisionId": revision["revisionId"],
            "value": value, "numerator": numerator, "denominator": len(samples), "unit": "ratio", "counts": [],
            "thresholdSnapshot": target, "judgmentStatus": "inconclusive" if reason else "passed" if passed else "failed",
            "reasonCode": reason, "formula": "已核验检查项 / 全部应检查项", "formulaCode": "coverage", "gap": None,
            "evidenceRefs": [f"audit-evidence-{run.run_id}-{index}"]})
    run.metric_results = results
    run.algorithm_mode = "audit_reference"
    run.judgment_status = "inconclusive" if any(r["judgmentStatus"] == "inconclusive" for r in results) else "failed" if any(r["judgmentStatus"] == "failed" for r in results) else "passed"
    run.integrity_state = "complete"
    entries = []
    for result in results:
        material = {"input": block, "inputHash": snapshot["auditEvaluationHash"], "source": snapshot["resolvedRefs"][0], "calculation": result}
        entries.append({"evidenceId": result["evidenceRefs"][0], "runId": run.run_id, "kind": "calculation", "sourceRef": material["source"],
            "sha256": digest(material), "integrityState": "complete", "missingFields": [], "redactedFields": [],
            "capturedAt": block["capturedAt"], "allowedActions": [], "mimeType": "application/json", "material": material})
    snapshot["auditEvidence"] = entries
    run.snapshot = snapshot


def evidence_entries(run):
    snapshot = run.snapshot or {}
    entries = deepcopy(snapshot.get("auditEvidence", []))
    valid = bool(snapshot.get("auditEvaluation")) and digest(snapshot["auditEvaluation"]) == snapshot.get("auditEvaluationHash")
    for entry in entries:
        m = entry.get("material", {})
        ok = valid and digest(m) == entry.get("sha256") and m.get("input") == snapshot["auditEvaluation"] and m.get("inputHash") == snapshot["auditEvaluationHash"] and m.get("source") == snapshot["resolvedRefs"][0] and m.get("calculation") in (run.metric_results or [])
        entry["integrityState"] = "complete" if ok else "corrupt"
    return entries
