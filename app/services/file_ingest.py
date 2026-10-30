"""本地文件接入记录的解析规则。

改之前：TXT、CSV、JSON 都把整份文件当成一条 dataset_records 记录，CSV 的多行
数据挤在同一个 payload.content 里，平台无法按表格行统计或逐条使用。
改之后：CSV 第一行作为列名，每个非空数据行单独生成一条记录，列名和值放在
该行 payload 中；TXT、JSON、XLSX、PDF 仍按整份文件各生成一条记录。
"""
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
    """解析已上传文件并返回待写入数据库的记录和文件总字节数。

    CSV 会忽略表头及全空行；遇到带引号的单元格内换行时，仍把整条表格记录
    当成一条样本。每条 CSV 样本的原始列值保留在 payload 顶层，文件名、行号
    和内容指纹放在 payload._source 中，避免与用户列名混在一起。

    TXT、JSON、XLSX、PDF 当前仍按文件保存为一条记录；XLSX/PDF 暂存整份文档，
    后续接入专用解析器后再拆分文档内部内容。
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
                # 改之前：只检查 CSV 有没有表头，随后把整个文件文本塞进一条记录。
                # 改之后：按 CSV 规则解析表格；一条非空数据行对应一条 payload，
                # 表头成为字段名，行号和指纹单独放进 _source，方便追溯原文件。
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
