"""Import a local scoring JSONL through the same persisted workflow as the UI."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from app.core.auth import permission_tier
from app.core.database import SessionLocal
from app.models.tables import SystemUser, Task
from app.services import agent_trace_ingest as ingest, agent_trace_competition as competition
from app.services import agent_trace_query as query


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--user-id", type=int, required=True, help="Existing operator user ID")
    parser.add_argument("--name", default="赛马数据 samples.jsonl")
    args = parser.parse_args()
    raw = args.path.read_bytes()
    if len(raw) > ingest.MAX_BYTES:
        parser.error("文件不能超过10MiB")
    records = [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    ids = [r.get("trajectory_id") for r in records]
    if not ids or any(not isinstance(i, str) or not i or i == "aggregate" for i in ids) or len(set(ids)) != len(ids):
        parser.error("trajectory_id 不能为空、重复或使用保留值 aggregate")
    for i, record in enumerate(records):
        ingest.normalize(record, "preflight", i, json.dumps(record, ensure_ascii=False), i + 1, "openai_messages")
    with SessionLocal() as db:
        user = db.get(SystemUser, args.user_id)
        if not user or user.status not in {"active", "normal"}:
            parser.error("用户不存在或已停用")
        actor = {"uid": user.id, "tier": permission_tier(user.role)}
        if not query.permissions(actor)["can_export"]:
            parser.error("此用户无导入权限")
        # Copy into backend storage: future parsing must not depend on a Desktop path.
        directory = Path(__file__).resolve().parents[1] / "storage" / "agent-trace-imports"
        directory.mkdir(parents=True, exist_ok=True)
        file_id = "file_" + uuid4().hex
        stored = directory / (file_id + ".jsonl")
        stored.write_bytes(raw)
        batch = ingest.create_batch(db, actor, args.name, uuid4().hex)
        ingest.register_asset(db, actor, file_id, args.path.name, stored, len(raw), hashlib.sha256(raw).hexdigest(), uuid4().hex)
        bound = ingest.bind(db, actor, batch["batch_id"], {"expected_version": 1,
            "files": [{"file_id": file_id, "format_override": "openai_messages"}]}, uuid4().hex)
        task, _ = ingest.submit(db, {"batch_id": batch["batch_id"], "expected_version": bound["batch"]["batch_version"]}, actor, uuid4().hex, "parse")
    ingest.run(task["task_id"])
    with SessionLocal() as db:
        result = db.get(Task, task["task_id"])
        if result.status != "succeeded":
            raise RuntimeError("导入未成功，请检查解析任务 " + task["task_id"])
        _, scope = query.resolve(db, batch["batch_id"], {})
        report = competition.report(db, scope, actor)
        output = directory / (batch["batch_id"] + "-submission.jsonl")
        output.write_text(report["jsonl"], encoding="utf-8")
        print(json.dumps({"batch_id": batch["batch_id"], "snapshot_id": scope["snapshot_id"],
                          "trajectory_ids": ids, "submission": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
