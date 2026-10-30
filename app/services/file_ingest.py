"""解析本地文件为 DatasetRecord payload；CSV 每个数据行对应一条记录。"""
from __future__ import annotations

import csv
from hashlib import sha256
import io
import json
from pathlib import Path
from typing import Any

UPLOAD_DIR = Path(__file__).resolve().parents[2] / "storage" / "uploads"


def _file_path(file_id: str) -> Path:
    if (
        not isinstance(file_id, str)
        or not file_id.startswith("file_")
        or not file_id.replace("_", "").isalnum()
    ):
        raise ValueError("上传文件标识无效")
    matches = [path for path in UPLOAD_DIR.glob(f"{file_id}.*") if path.is_file()]
    if len(matches) != 1:
        raise ValueError(f"上传文件不存在或已失效：{file_id}")
    return matches[0]


def _validate_json(content: str, path: Path) -> None:
    try:
        json.loads(content)
    except json.JSONDecodeError:
        # JSON Lines is commonly exported by data platforms and is safe to
        # support without treating a malformed JSON document as a single row.
        try:
            for line in content.splitlines():
                if line.strip():
                    json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"JSON 文件格式无效：{path.name}") from error


def read_uploaded_file_rows(file_ids: object) -> tuple[list[dict[str, Any]], int]:
    """Return DB-ready records; CSV becomes one record per non-empty data row.

    XLSX/PDF are stored as a document record until a dedicated document parser
    is configured; their binary is never placed in an in-memory task result.
    """
    if not isinstance(file_ids, list) or not file_ids:
        raise ValueError("本地文件接入至少需要一个已上传文件")

    rows: list[dict[str, Any]] = []
    total_bytes = 0
    for file_id in file_ids:
        path = _file_path(file_id)
        raw = path.read_bytes()
        total_bytes += len(raw)
        extension = path.suffix.lower()
        payload: dict[str, Any] = {
            "storage_granularity": "file",
            "source_file_id": file_id,
            "source_filename": path.name,
            "source_extension": extension,
            "document_size_bytes": len(raw),
            # 原文件内容决定指纹，上传标识和文件名不参与重复检测。
            "source_content_hash": sha256(raw).hexdigest(),
        }
        if extension in {".txt", ".csv", ".json"}:
            try:
                content = raw.decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise ValueError(f"文件必须使用 UTF-8 编码：{path.name}") from error
            if extension == ".csv":
                # 以前把整份 CSV 当成一条记录保存；现在跳过表头和空行，
                # 每个数据行单独保存，列名和值原样放进该行的 payload。
                reader = csv.DictReader(io.StringIO(content), strict=True)
                fieldnames = reader.fieldnames
                if not fieldnames or any(not (name or "").strip() for name in fieldnames):
                    raise ValueError(f"CSV 文件缺少表头：{path.name}")
                if len(set(fieldnames)) != len(fieldnames):
                    raise ValueError(f"CSV 文件包含重复列名：{path.name}")
                if "_source" in fieldnames:
                    raise ValueError(f"CSV 文件使用了系统保留列名 _source：{path.name}")

                csv_rows = []
                for row_number, source_row in enumerate(reader, start=2):
                    if None in source_row:
                        raise ValueError(f"CSV 第 {row_number} 行列数多于表头：{path.name}")
                    row = {key: value for key, value in source_row.items()}
                    if not any(value is not None and str(value).strip() for value in row.values()):
                        continue
                    canonical = json.dumps(
                        row, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    row_hash = sha256(canonical.encode("utf-8")).hexdigest()
                    csv_rows.append({
                        **row,
                        "_source": {
                            "storage_granularity": "row",
                            "source_file_id": file_id,
                            "source_filename": path.name,
                            "source_extension": extension,
                            "source_row_number": row_number,
                            "content_hash": row_hash,
                        },
                    })
                rows.extend(csv_rows)
                continue
            elif extension == ".json":
                _validate_json(content, path)
            # TXT/JSON 保留原始文本；一个文件仍作为一条文档记录。
            payload["content"] = content
        rows.append(payload)
    return rows, total_bytes
