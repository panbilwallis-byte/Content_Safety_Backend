"""FashionMNIST 图像样本抽样导入 datasets / dataset_records / tasks（#2.2）。

用法（在 Content_Safety_Backend 目录下）：
    uv run python scripts/import_fashion_samples.py                 # 独立分层抽样 500 条
    uv run python scripts/import_fashion_samples.py --count 500 --seed 42
    uv run python scripts/import_fashion_samples.py --run-dir "E:/.../db/training-runs/<任务ID>"
    uv run python scripts/import_fashion_samples.py <同上> --reset   # 覆盖式重导

约定：
- 数据源是训练脚本同一份官方 IDX 缓存（默认找同级仓库 content-security-llm 的训练数据目录），
  不另下载、不用合成图；
- --run-dir 指向一次真实训练任务的产物目录时，样本直接取该次 split.json 记录的索引，
  平台里的每一行都能与那次训练消费的样本逐号对上；不带该参数则按同一公式独立分层抽样；
- content_hash = sha256(像素字节 + 类别)，重复导入同一张图判重跳过；
- 幂等：同名同版本数据集已存在且未加 --reset 时直接退出。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import random
import struct
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, select

from app.core.database import SessionLocal
from app.core.time import now_shanghai
from app.models.tables import Dataset, DatasetRecord, Task
from app.services.dataset_version_service import create_ingest_version, purge_dataset_ledger
from app.services.lineage_service import record_ingest_edge

DATASET_NAME = "FashionMNIST 图像样本集"
DATASET_VERSION = "v1.0.0"
SOURCE_LABEL = "FashionMNIST 官方图像数据集（Zalando Research）"
LABELS = [
    {"index": 0, "zh": "T恤/上衣", "en": "T-shirt/top"},
    {"index": 1, "zh": "长裤", "en": "Trouser"},
    {"index": 2, "zh": "套头衫", "en": "Pullover"},
    {"index": 3, "zh": "连衣裙", "en": "Dress"},
    {"index": 4, "zh": "外套", "en": "Coat"},
    {"index": 5, "zh": "凉鞋", "en": "Sandal"},
    {"index": 6, "zh": "衬衫", "en": "Shirt"},
    {"index": 7, "zh": "运动鞋", "en": "Sneaker"},
    {"index": 8, "zh": "手提包", "en": "Bag"},
    {"index": 9, "zh": "短靴", "en": "Ankle boot"},
]
DEFAULT_RAW_DIRS = (
    Path(__file__).resolve().parents[2] / "content-security-llm" / "db" / "training-data"
    / "fashion-cnn" / "FashionMNIST" / "raw",
)
BATCH = 200


def find_raw_dir(explicit: Path | None) -> Path:
    if explicit is not None:
        candidates = [explicit]
    else:
        candidates = list(DEFAULT_RAW_DIRS)
    for directory in candidates:
        if (directory / "train-images-idx3-ubyte").exists() or (directory / "train-images-idx3-ubyte.gz").exists():
            return directory
    raise SystemExit("未找到 FashionMNIST IDX 缓存，请用 --data-dir 指定（内含 train-images-idx3-ubyte[.gz]）")


def open_idx(directory: Path, stem: str, header_ints: int):
    """返回 IDX 头部整数与停在数据区起点的二进制句柄（优先未压缩文件，其次 .gz）。"""
    plain = directory / stem
    path = plain if plain.exists() else directory / (stem + ".gz")
    if not path.exists():
        raise SystemExit(f"缺少数据文件：{path}")
    handle = (gzip.open if path.suffix == ".gz" else open)(path, "rb")
    header = list(struct.unpack(">" + "I" * header_ints, handle.read(4 * header_ints)))
    if header[0] not in (2049, 2051):
        raise SystemExit(f"{path} 不是 IDX 文件（magic={header[0]}）")
    return header, handle


def stratified_indices(labels: bytes, count: int, seed: int) -> list[int]:
    """每类等额抽样：固定种子把每类索引洗牌后取前 N 条，与训练脚本的抽样口径一致但不必逐号相同。"""
    rng = random.Random(seed)
    chosen: list[int] = []
    for label in range(len(LABELS)):
        pool = [index for index, value in enumerate(labels) if value == label]
        quota = count // len(LABELS) + (label < count % len(LABELS))
        if quota > len(pool):
            raise SystemExit(f"类别 {LABELS[label]['zh']} 只有 {len(pool)} 条，取不到配额 {quota}")
        rng.shuffle(pool)
        chosen.extend(pool[:quota])
    return sorted(chosen)


def run_split_indices(run_dir: Path) -> list[int]:
    path = run_dir / "split.json"
    if not path.exists():
        raise SystemExit(f"训练产物目录缺少 split.json：{path}")
    return list(json.loads(path.read_text(encoding="utf-8"))["train_indices"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, help="FashionMNIST raw 目录（默认取训练脚本同一份缓存）")
    parser.add_argument("--count", type=int, default=500, help="导入条数，默认每类 50 条共 500 条")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--run-dir", type=Path, help="某次真实训练任务产物目录，直接复用其 split.json 索引")
    parser.add_argument("--reset", action="store_true", help="删除同名同版本数据集及样本后重导")
    args = parser.parse_args()
    if args.count < len(LABELS):
        raise SystemExit("条数至少要覆盖每类一条")

    raw_dir = find_raw_dir(args.data_dir)
    image_header, image_handle = open_idx(raw_dir, "train-images-idx3-ubyte", 4)
    _, label_handle = open_idx(raw_dir, "train-labels-idx1-ubyte", 2)
    count_items, rows, cols = image_header[1], image_header[2], image_header[3]
    pixel_size = rows * cols
    with image_handle, label_handle:
        pixels_all = image_handle.read(count_items * pixel_size)
        labels_all = label_handle.read(count_items)
    if args.run_dir is not None:
        pool = [index for index in run_split_indices(args.run_dir) if index < len(labels_all)]
        if len(pool) > args.count:
            picker = random.Random(args.seed)
            picker.shuffle(pool)
            pool = pool[:args.count]
        indices = sorted(pool)
        mode = f"复用训练任务 {args.run_dir.name} 的 split.json（取其中 {len(indices)} 条）"
    else:
        indices = stratified_indices(labels_all, args.count, args.seed)
        mode = f"独立分层抽样（seed={args.seed}）"

    with SessionLocal() as db:
        existing = db.scalar(select(Dataset).where(
            Dataset.name == DATASET_NAME, Dataset.version == DATASET_VERSION))
        if existing and not args.reset:
            print(f"数据集已存在（id={existing.id}，{existing.name} {existing.version}），"
                  "如需覆盖重导请加 --reset")
            return 1
        if existing and args.reset:
            purge_dataset_ledger(db, existing.id)
            db.execute(delete(DatasetRecord).where(DatasetRecord.dataset_id == existing.id))
            db.delete(existing)
            db.flush()

        dataset = Dataset(
            name=DATASET_NAME, category="图像样本", source_type="industry",
            version=DATASET_VERSION, status="ready",
            description=("FashionMNIST 官方灰度图像（28×28，10 类服饰）的真实抽样，"
                         f"作为 CNN 训练与推理演示的样本仓；每行存原始像素与类别，"
                         f"来源方式：{mode}。"),
            metadata_json={"languages": [], "modalities": ["图像"], "purpose": "training",
                           "source_name": SOURCE_LABEL, "owner": "课题组协作",
                           "quality_status": "good", "quality_score": 95,
                           "image_size": 28, "classes": [label["zh"] for label in LABELS]})
        db.add(dataset)
        db.flush()

        task_id = f"TR-{uuid.uuid4().hex[:8]}"
        seen: set[str] = set()
        imported = duplicates = 0
        buffer: list[DatasetRecord] = []
        for index in indices:
            label = int(labels_all[index])
            pixels = pixels_all[index * pixel_size:(index + 1) * pixel_size]
            digest = hashlib.sha256(pixels + bytes([label])).hexdigest()
            if digest in seen:
                duplicates += 1
                continue
            seen.add(digest)
            buffer.append(DatasetRecord(
                dataset_id=dataset.id, task_id=task_id, content_hash=digest,
                payload={"modality": "image", "image_size": rows, "image_channels": 1,
                         "pixels": list(pixels),
                         "label_index": label, "label": LABELS[label]["zh"],
                         "label_en": LABELS[label]["en"], "text": "",
                         "source_dataset": "FashionMNIST/train", "source_index": int(index),
                         "training_run": args.run_dir.name if args.run_dir else None,
                         "source_library": "gu-yyt-lab/MajorDataGovernanceProject 图像抽样"}))
            imported += 1
            if len(buffer) >= BATCH:
                db.add_all(buffer)
                db.flush()
                buffer.clear()
        if buffer:
            db.add_all(buffer)
            db.flush()

        storage_gb = round(max(imported * pixel_size / 1_000_000_000, 0.001), 3)
        dataset.metadata_json = {**dataset.metadata_json,
                                 "record_count": imported, "storage_gb": storage_gb}
        db.add(Task(
            task_id=task_id, name="FashionMNIST 图像抽样导入", capability_code="data_ingest",
            trace_id=uuid.uuid4().hex, status="succeeded", source_name=SOURCE_LABEL,
            dataset_name=DATASET_NAME, storage_gb=storage_gb, progress=100,
            success_count=imported, duplicate_count=duplicates, anomaly_count=0,
            input_data={"connectorType": "file", "sourceType": "industry", "createDataset": True,
                        "files": [str(raw_dir)], "languages": [], "modalities": ["图像"],
                        "sourceName": SOURCE_LABEL, "owner": "课题组协作"},
            config={}, result={"imported": imported, "duplicates": duplicates,
                               "dataset_id": dataset.id,
                               "sampling": {"mode": mode, "count": args.count, "seed": args.seed,
                                            "run_dir": str(args.run_dir) if args.run_dir else None}},
            dataset_version=DATASET_VERSION,
            created_at=now_shanghai(), finished_at=now_shanghai()))
        db.flush()
        # 改之前：导入脚本把新样本并入原始版本；改之后：登记完整新版本，旧版本保持原样。
        create_ingest_version(db, dataset, task_id=task_id)
        record_ingest_edge(db, task_id=task_id, dataset=dataset)
        db.commit()
        print(f"导入完成：数据集 id={dataset.id}，样本 {imported} 条，重复跳过 {duplicates} 条，"
              f"抽样方式={mode}，任务 {task_id}，存储 {storage_gb} GB")
        return 0


if __name__ == "__main__":
    sys.exit(main())
