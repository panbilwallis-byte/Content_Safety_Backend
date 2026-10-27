"""一次性修复历史清洗任务的对比行编号（idempotent）。

背景：旧版真清洗写对比行时用了 "R-{记录号}" 前缀并把原文截到 120 字，
前端按记录号回查版本清单时对不上，整份详情报"任务详情加载失败"。
本脚本把 data_process 任务 result.comparisons 中的旧编号改回真实记录号，
并从 dataset_records 补回完整原文；已修复的行原样跳过。

用法（在 Content_Safety_Backend 目录下）：
    uv run python scripts/fix_process_comparisons.py --dry-run   # 先看会改什么
    uv run python scripts/fix_process_comparisons.py
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models.tables import DatasetRecord, Task

LEGACY_ID = re.compile(r"^R-(\d+)$")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    with SessionLocal() as db:
        tasks = db.scalars(select(Task).where(Task.capability_code == "data_process")).all()
        fixed_tasks = 0
        fixed_rows = 0
        for task in tasks:
            result = dict(task.result or {})
            comparisons = result.get("comparisons") or []
            changed = False
            for row in comparisons:
                match = LEGACY_ID.match(str(row.get("id") or ""))
                if not match:
                    continue
                record_id = int(match.group(1))
                row["id"] = str(record_id)
                record = db.get(DatasetRecord, record_id)
                text = str((record.payload or {}).get("text") or "") if record else ""
                if text and row.get("original") != text:
                    row["original"] = text
                changed = True
                fixed_rows += 1
            if changed:
                fixed_tasks += 1
                if not args.dry_run:
                    task.result = {**result, "comparisons": comparisons}
        if not args.dry_run:
            db.commit()
        print(f"{'[dry-run] ' if args.dry_run else ''}涉及任务 {fixed_tasks} 个，对比行 {fixed_rows} 条")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
