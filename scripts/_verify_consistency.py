# -*- coding: utf-8 -*-
"""独立验证：绕过写入脚本，用原生 SQL 读取四张表并独立重算 manifest_hash。"""
import hashlib
import sys

sys.path.insert(0, r"D:\数据治理\Content_Safety_Backend")

from sqlalchemy import text
from app.core.database import engine

problems = []

with engine.connect() as conn:
    datasets = conn.execute(text(
        "SELECT id, name FROM datasets ORDER BY id"
    )).mappings().all()
    print(f"datasets 数量: {len(datasets)}")

    for ds in datasets:
        did, dname = ds["id"], ds["name"]
        # 1) 记录数（全部 + 原件 + 模拟批次）
        rec = conn.execute(text(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN source_record_id IS NULL THEN 1 ELSE 0 END) AS raw_count "
            "FROM dataset_records WHERE dataset_id = :did"), {"did": did}).mappings().one()
        # 2) 版本卡
        ver = conn.execute(text(
            "SELECT version_id, label, sample_count, manifest_hash, producing_task_id "
            "FROM dataset_versions WHERE dataset_id = :did ORDER BY version_id"), {"did": did}).mappings().all()
        # 3) 版本条目数与内容指纹
        items = conn.execute(text(
            "SELECT vi.version_id, vi.record_id, vi.content_hash, "
            "r.content_hash AS record_content_hash, r.source_record_id "
            "FROM dataset_version_items vi "
            "LEFT JOIN dataset_records r ON r.id = vi.record_id "
            "WHERE vi.version_id IN (SELECT version_id FROM dataset_versions WHERE dataset_id = :did) "
            "ORDER BY vi.version_id, vi.record_id"), {"did": did}).mappings().all()
        # 4) 模拟批次 task_id 集合
        tasks = conn.execute(text(
            "SELECT DISTINCT task_id FROM dataset_records WHERE dataset_id = :did"), {"did": did}).mappings().all()

        print(f"\n--- dataset_id={did} {dname} ---")
        print(f"records: total={rec['total']} raw={rec['raw_count']} 模拟task: {[t['task_id'] for t in tasks]}")
        for v in ver:
            vitems = [it for it in items if it["version_id"] == v["version_id"]]
            digest = hashlib.sha256()
            for it in sorted(vitems, key=lambda x: x["record_id"]):
                digest.update("{}:{}\n".format(it["record_id"], it["content_hash"] or "").encode("utf-8"))
            recomputed = digest.hexdigest()
            print(f"  版本 {v['version_id']} label={v['label']} sample_count={v['sample_count']} "
                  f"items={len(vitems)} producing_task={v['producing_task_id']}")
            print(f"  manifest 存储={v['manifest_hash'][:16]}... 重算={recomputed[:16]}...")
            if v["sample_count"] != len(vitems):
                problems.append(f"{v['version_id']} sample_count({v['sample_count']}) != items({len(vitems)})")
            if v["manifest_hash"] != recomputed:
                problems.append(f"{v['version_id']} manifest_hash 不一致")
            for it in vitems:
                if it["content_hash"] != it["record_content_hash"]:
                    problems.append(f"{v['version_id']} record {it['record_id']} 指纹与 records 不一致")
                if it["source_record_id"] is not None:
                    problems.append(f"{v['version_id']} record {it['record_id']} 不是原件行")
        if rec["total"] != 10:
            problems.append(f"dataset {did} records={rec['total']} != 10")

    # 5) 全库重复指纹检查（模拟批次内）
    dup = conn.execute(text(
        "SELECT content_hash, COUNT(*) c FROM dataset_records "
        "WHERE task_id LIKE 'tsk_sim_seed_%' GROUP BY content_hash HAVING c > 1")).mappings().all()
    if dup:
        problems.append(f"模拟批次存在重复指纹: {len(dup)} 组")

    # 6) (version_id, record_id) 唯一约束是否生效
    dupe_items = conn.execute(text(
        "SELECT version_id, record_id, COUNT(*) c FROM dataset_version_items "
        "GROUP BY version_id, record_id HAVING c > 1")).mappings().all()
    if dupe_items:
        problems.append(f"版本条目存在重复: {len(dupe_items)} 组")

print("\n" + "=" * 40)
if problems:
    print("[FAIL]")
    for p in problems:
        print("  - " + p)
    sys.exit(1)
print("[PASS] 全部一致性校验通过：数量、指纹、manifest、唯一性均一致。")
