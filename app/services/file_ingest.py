"""本地文件按整份文档入库，每个文件对应一条 DatasetRecord。"""
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
    """Return one DB-ready record per file, preserving complete text content.

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
                if not csv.DictReader(io.StringIO(content)).fieldnames:
                    raise ValueError(f"CSV 文件缺少表头：{path.name}")
            elif extension == ".json":
                _validate_json(content, path)
            # 保留原始文本与换行；CSV/JSON 同样以整个文件为单位保存。
            payload["content"] = content
        rows.append(payload)
    return rows, total_bytes
