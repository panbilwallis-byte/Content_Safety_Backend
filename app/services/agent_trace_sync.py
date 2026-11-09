"""Background-only import of frozen value results. GET handlers never synthesize data."""
import json
import logging
import os
import threading
from datetime import datetime, timezone, timedelta
from hashlib import sha256

from fastapi import HTTPException
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionLocal
from app.core.time import now_shanghai
from app.models.tables import Task
from app.models.agent_traces import (
    TraceBatch, TraceSnapshot, TraceProjection, TraceSample, TraceEvent, TraceCall,
    TraceRelation, TraceEvidence, TraceIngestCheckpoint,
)
from app.services.agent_trace_generation import VERSION, generated_sample, stable_id, validate_process

log = logging.getLogger(__name__)


def decode(value, default=None):
    return json.loads(value) if isinstance(value, str) else value if value is not None else default


def checkpoint(db, key, status, message=""):
    row = db.get(TraceIngestCheckpoint, key)
    if row is None:
        row = TraceIngestCheckpoint(source_key=key)
        db.add(row)
    row.status, row.message, row.updated_at = status, message[:1000], now_shanghai()


def llm_owner():
    token = os.getenv("LLM_ACCESS_TOKEN", "")
    if not token:
        raise HTTPException(503, "未配置 LLM_ACCESS_TOKEN，模型服务价值结果未接入")
    return sha256(token.encode()).hexdigest()


def check_llm_token(connection, owner):
    row = connection.execute(text(
        "SELECT enabled,expires_at,deleted_at FROM llm_auth_tokens WHERE token_hash=:owner"),
        {"owner": owner}).mappings().first()
    if not row or not row["enabled"] or row["deleted_at"]:
        raise HTTPException(403, "模型服务来源令牌已停用或不存在")
    expires = datetime.fromisoformat(str(row["expires_at"]))
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= datetime.now(timezone.utc):
        raise HTTPException(403, "模型服务来源令牌已过期")


def ingest(db, source, samples):
    """Publish all records in the caller's transaction, including an empty valid result."""
    key = source["system"] + ":" + source["task_id"]
    batch_id = stable_id("batch_", key)
    if db.get(TraceBatch, batch_id):
        return batch_id
    if not isinstance(samples, list) or any(not isinstance(s, dict) or not str(s.get("id", "")).strip() for s in samples):
        raise ValueError("价值结果没有可靠的逐样本明细或样本 ID")
    if len({str(s["id"]) for s in samples}) != len(samples):
        raise ValueError("价值结果样本 ID 重复")
    snap = stable_id("snap_", key, source["result_id"])
    batch = TraceBatch(batch_id=batch_id, source_system=source["system"], source_task_id=source["task_id"],
        source_token_hash=source.get("token_hash"), display_id="AUD-" + batch_id[-10:].upper(),
        active_snapshot_id=snap, payload={"scope": source.get("scope", {}), "value_result_id": source["result_id"],
            "name": source.get("name", "数据价值分析"), "data_origin": "mixed"})
    db.add(batch)
    db.add(TraceSnapshot(snapshot_id=snap, batch_id=batch_id, payload={
        "schema_version": "1", "value_result_id": source["result_id"], "source_scope": source.get("scope", {}),
        "model_version": source.get("model_version"), "sample_count": len(samples), "data_origin": "mixed"}))
    db.add(TraceProjection(projection_id=stable_id("proj_", snap, VERSION), snapshot_id=snap, version=VERSION,
                           payload={"policy_version": VERSION, "data_origin": "mixed"}))
    for index, original in enumerate(samples):
        sample = {**original, "id": str(original["id"])}
        trace, payload, events, calls, relations = generated_sample(key, sample, index)
        validate_process(payload, events, calls)
        payload.update(sample_revision_id=sample.get("revision_id") or
            f"{source.get('scope', {}).get('version_id', source['result_id'])}:{sample['id']}",
            content_hash=sha256(str(sample.get("text", "")).encode()).hexdigest())
        db.add(TraceSample(id=stable_id("sample_", snap, trace), snapshot_id=snap, trace_id=trace,
            sample_id=sample["id"], display_id=f"AUD-{index + 1:04}", ingest_order=index,
            language=str(sample.get("language") or "unknown"), result=sample, payload=payload))
        for e in events:
            db.add(TraceEvent(id=stable_id("row_", snap, trace, e["event_id"]), snapshot_id=snap,
                trace_id=trace, event_id=e["event_id"], ordinal=e["ordinal"], payload=e))
        for c in calls:
            db.add(TraceCall(id=stable_id("row_", snap, trace, c["call_id"]), snapshot_id=snap,
                trace_id=trace, call_id=c["call_id"], ordinal=c["order"], payload=c))
            if c.get("data_origin") == "generated" and c["permission_decision"] == "denied":
                evidence_id = stable_id("evidence_", snap, trace, c["call_id"])
                db.add(TraceEvidence(id=evidence_id, snapshot_id=snap, projection_version=VERSION,
                    trace_id=trace, event_id=c["event_id"], payload={
                        "evidence_id": evidence_id, "event_id": c["event_id"],
                        "risk_type": {"code": "simulated_overreach", "label": "模拟越权场景"},
                        "match_pattern": "模拟请求超出 public.read 范围", "severity": "high",
                        "rule_id": "simulation-permission", "rule_version": VERSION, "policy_version": VERSION,
                        "review_status": "not_required", "confidence": None, "confidence_kind": "unknown",
                        "related_call_ids": [c["call_id"]], "data_origin": "generated"}))
        for relation in relations:
            db.add(TraceRelation(id=stable_id("row_", snap, relation["edge_id"]),
                snapshot_id=snap, trace_id=trace, payload=relation))
    checkpoint(db, key, "succeeded")
    db.flush()
    return batch_id


class TraceSynchronizer:
    def __init__(self, sessions=SessionLocal):
        self.sessions = sessions
        self.stop_event = threading.Event()
        self.thread = None
        url = os.getenv("AGENT_TRACE_LLM_DATABASE_URL")
        self.external = create_engine(url, pool_pre_ping=True, echo=False) if url else None

    def record(self, key, status, message):
        with self.sessions() as db:
            checkpoint(db, key, status, message)
            db.commit()

    def publish(self, source, samples):
        key = source["system"] + ":" + source["task_id"]
        try:
            with self.sessions() as db:
                ingest(db, source, samples)
                db.commit()
        except IntegrityError:
            # A concurrent worker may have committed the same deterministic source.
            with self.sessions() as db:
                if db.get(TraceBatch, stable_id("batch_", key)):
                    return
            self.record(key, "failed", "导入记录约束校验失败")
        except (ValueError, KeyError, TypeError):
            self.record(key, "failed", "结果明细或执行过程不完整，请检查来源样本和事件关联")

    def sync_local(self):
        with self.sessions() as db:
            tasks = list(db.scalars(select(Task).where(Task.capability_code.in_(["value_score", "high_value_detect"]),
                                                       Task.status == "succeeded")))
            sources = [(t.task_id, t.name, t.input_data, t.result, t.model_version) for t in tasks]
        for tid, name, scope, result, model_version in sources:
            if self.stop_event.is_set():
                return
            result = result or {}
            # Do not use the legacy endpoint which reconstructs fictitious sample identities.
            samples = result.get("samples")
            self.publish({"system": "business", "task_id": tid, "result_id": str(result.get("id") or tid),
                          "name": name, "scope": scope or {}, "model_version": model_version}, samples)

    def sync_llm(self):
        owner = llm_owner()
        context = self.external.connect() if self.external is not None else self.sessions()
        with context as db:
            check_llm_token(db, owner)
            rows = list(db.execute(text("""SELECT r.id,r.task_id,r.samples_json,r.summary_json,
                t.scope_json,t.model_version FROM llm_governance_results r
                JOIN llm_governance_tasks t ON t.id=r.task_id
                WHERE r.kind='governance-value' AND t.kind='governance-value' AND t.status='succeeded'
                AND r.token_hash=:owner AND t.token_hash=:owner ORDER BY r.created_at,r.id"""),
                {"owner": owner}).mappings())
            for row in rows:
                if self.stop_event.is_set():
                    return
                samples = decode(row["samples_json"])
                outcomes = list(db.execute(text("SELECT sample_id,status,output_json,error_code "
                    "FROM llm_governance_task_samples WHERE task_id=:tid ORDER BY sample_id"),
                    {"tid": row["task_id"]}).mappings())
                if isinstance(samples, list):
                    samples = [dict(s) for s in samples]
                    by_id = {str(s.get("id")): s for s in samples}
                    for outcome in outcomes:
                        sid = str(outcome["sample_id"])
                        output = decode(outcome["output_json"], {})
                        if sid in by_id:
                            by_id[sid]["source_output"] = output
                        else:
                            samples.append({"id": sid, "status": outcome["status"], "source_output": output,
                                            "error_code": outcome["error_code"], "language": "unknown"})
                self.publish({"system": "llm", "task_id": row["task_id"], "result_id": row["id"],
                              "scope": decode(row["scope_json"], {}), "token_hash": owner,
                              "model_version": row["model_version"]}, samples)

    def tick(self):
        for system, sync in (("business", self.sync_local), ("llm", self.sync_llm)):
            try:
                sync()
                self.record("system:" + system, "succeeded", "来源检查完成")
            except HTTPException as exc:
                self.record("system:" + system, "unavailable", str(exc.detail))
            except Exception:
                # Do not print SQL parameters, connection credentials or source texts.
                self.record("system:" + system, "unavailable", "来源读取失败，请检查数据库连接、表结构与权限")
                log.warning("Agent trace %s source unavailable", system)
        from app.services.agent_trace_statistics import materialize
        while not self.stop_event.is_set():
            with self.sessions() as db:
                count = materialize(db)
                db.commit()
            if not count:
                break
        from app.services.agent_trace_export import run_export, directory
        from app.services.agent_trace_ingest import run as run_ingest
        from app.models.agent_traces import TraceArtifact
        with self.sessions() as db:
            pending = list(db.execute(select(Task.task_id, Task.capability_code).where(Task.capability_code.in_(["agent_trace_export", "agent_trace_ingest"]), Task.status == "pending")))
            # A crash during file generation must not leave an indefinitely running task.
            for task in db.scalars(select(Task).where(Task.capability_code == "agent_trace_export",
                    Task.status == "running", Task.created_at < now_shanghai() - timedelta(hours=1))):
                task.status, task.finished_at = "failed", now_shanghai()
                task.result = {**(task.result or {}), "error_message": "导出执行中断或超时，请重新导出", "artifacts": []}
            for artifact in db.scalars(select(TraceArtifact).where(TraceArtifact.expires_at <= now_shanghai())):
                path = directory() / artifact.storage_name
                if path.parent == directory():
                    path.unlink(missing_ok=True)
                db.delete(artifact)
            db.commit()
        for task_id, capability in pending:
            if self.stop_event.is_set():
                break
            (run_ingest if capability == "agent_trace_ingest" else run_export)(task_id, self.sessions)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                log.warning("Agent trace synchronization temporarily unavailable")
            self.stop_event.wait(10)

    def start(self):
        self.thread = threading.Thread(target=self.run, name="agent-trace-sync", daemon=True)
        self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        if self.external:
            self.external.dispose()
