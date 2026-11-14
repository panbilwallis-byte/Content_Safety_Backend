"""Dry-run by default. --apply requires an explicit metadata backup destination."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.database import SessionLocal
from app.services.internal_agent_merge import merge_runs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", action="append", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup")
    args = parser.parse_args()
    if args.apply and not args.backup:
        parser.error("--apply requires --backup")
    with SessionLocal() as db:
        plan = merge_runs(db, args.run, args.name)
        if args.apply and not plan.get("already_merged"):
            backup = Path(args.backup)
            backup.parent.mkdir(parents=True, exist_ok=True)
            with backup.open("x", encoding="utf-8") as handle:
                json.dump(plan, handle, ensure_ascii=False, indent=2)
            plan = merge_runs(db, args.run, args.name, apply=True)
            db.commit()
        else:
            db.rollback()
        print(json.dumps({k: plan[k] for k in ("applied", "name", "batch_id", "trace_count")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
