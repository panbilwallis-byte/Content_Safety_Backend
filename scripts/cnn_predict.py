"""调用本地 CNN 推理服务（#2.3 演示工具）：从平台样本取像素，打 8010 的 /predict。

用法（在 Content_Safety_Backend 目录下）：
    .venv\\Scripts\\python.exe scripts/cnn_predict.py --record 33119
    .venv\\Scripts\\python.exe scripts/cnn_predict.py --random 20
    .venv\\Scripts\\python.exe scripts/cnn_predict.py --random 50 --dataset 9 --seed 7

约定：
- 服务地址默认 http://127.0.0.1:8010，先探 /healthz 再逐条调用，服务没起来直接给启动命令；
- 真值取样本 payload 里的 label（导入时来自官方标签），命中与否逐条打印，批量给命中率；
- 只读：不写任何表，纯演示与核对用。
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import SessionLocal, engine
from app.models.tables import Dataset, DatasetRecord

engine.echo = False  # 只读核对工具，不需要把 SQL 刷进演示输出

PIXEL_COUNT = 784


def pixels_of(record: DatasetRecord) -> tuple[list[int], str] | None:
    payload = record.payload or {}
    pixels = payload.get("pixels")
    if not isinstance(pixels, list) or len(pixels) != PIXEL_COUNT:
        return None
    return [int(value) for value in pixels], str(payload.get("label") or payload.get("label_en") or "?")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8010", help="推理服务地址")
    parser.add_argument("--record", type=int, help="按 dataset_records.id 调用单条样本")
    parser.add_argument("--random", type=int, dest="sample_count", help="随机抽 N 条图像样本批量核对")
    parser.add_argument("--dataset", type=int, default=9, help="随机抽样的数据集 ID，默认 9（FashionMNIST 图像样本集）")
    parser.add_argument("--seed", type=int, default=42, help="随机种子：同一 --seed 抽到同一批样本")
    parser.add_argument("--top-k", type=int, default=3, help="返回前几名候选")
    args = parser.parse_args()
    if args.record is None and not args.sample_count:
        parser.error("请给 --record <样本ID> 或 --random <条数>")

    try:
        health = httpx.get(f"{args.url}/healthz", timeout=10)
    except httpx.HTTPError as exc:
        print(f"推理服务连不上（{args.url}）：{exc.__class__.__name__}")
        print("启动它：cd content-security-llm && .venv\\Scripts\\python.exe service\\fashion_serve.py --port 8010")
        return 1
    if health.status_code != 200:
        print(f"推理服务未就绪（{health.status_code}）：{health.text[:200]}")
        return 1
    info = health.json()
    print("服务：{} | 权重 {} sha256 {}… | torch {} | {}".format(
        info.get("service"), info.get("weights"), (info.get("weights_sha256") or "")[:12],
        info.get("torch"), info.get("device")))

    with SessionLocal() as db:
        if args.record is not None:
            record = db.get(DatasetRecord, args.record)
            if record is None:
                print(f"样本不存在：dataset_records.id={args.record}")
                return 1
            records = [record]
        else:
            rows = list(db.scalars(select(DatasetRecord).where(
                DatasetRecord.dataset_id == args.dataset).order_by(DatasetRecord.id)))
            images = [row for row in rows if pixels_of(row) is not None]
            if not images:
                dataset = db.get(Dataset, args.dataset)
                print(f"数据集 {args.dataset}（{dataset.name if dataset else '不存在'}）里没有图像样本，"
                      "请用 --dataset 指向 FashionMNIST 图像样本集")
                return 1
            picker = random.Random(args.seed)
            records = picker.sample(images, min(args.sample_count, len(images)))
        dataset_names = {dataset.id: dataset.name for dataset in db.scalars(select(Dataset))}

        hit = 0
        elapsed: list[float] = []
        for record in records:
            prepared = pixels_of(record)
            if prepared is None:
                print(f"  跳过 id={record.id}：payload 里没有 {PIXEL_COUNT} 个像素")
                continue
            pixels, truth = prepared
            began = time.perf_counter()
            answer = httpx.post(f"{args.url}/predict", json={"pixels": pixels, "top_k": args.top_k},
                                timeout=60).json()
            round_trip_ms = (time.perf_counter() - began) * 1000
            correct = answer.get("labelZh") == truth
            hit += correct
            elapsed.append(float(answer.get("elapsedMs") or 0))
            print("  样本 {:<7} 数据集{:<3} 真值={:<6} 预测={:<6} 置信度={:<8} 服务端={:<7} 往返={:<7} {}".format(
                record.id, record.dataset_id, truth, answer.get("labelZh"),
                round(float(answer.get("confidence") or 0), 4),
                "{}ms".format(answer.get("elapsedMs")), "{}ms".format(round(round_trip_ms, 1)),
                "对" if correct else "错"))
            if args.top_k and args.top_k > 1:
                ranking = " | ".join("{} {:.3f}".format(item["labelZh"], item["probability"])
                                     for item in answer.get("ranking") or [])
                print("            候选：{}".format(ranking))

    if len(records) > 1 and elapsed:
        print("命中率 {}/{} = {:.1f}%（数据集：{}）；服务端耗时 平均 {:.2f}ms / 最大 {:.2f}ms".format(
            hit, len(elapsed), 100 * hit / len(elapsed),
            dataset_names.get(args.dataset, args.dataset),
            sum(elapsed) / len(elapsed), max(elapsed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
