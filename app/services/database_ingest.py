"""MySQL 数据源读取并写入平台数据集的连接器。"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.models.tables import DatasetRecord

MAX_DATABASE_ROWS = 100_000
FETCH_SIZE = 500
TABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
READ_ONLY_QUERY = re.compile(r"^\s*(?:select|with)\b", re.IGNORECASE)
FROM_CLAUSE = re.compile(r"\bfrom\b", re.IGNORECASE)
FORBIDDEN_SQL = re.compile(
    r";|--|/\*|\b(?:insert|update|delete|drop|alter|create|grant|revoke|truncate|call)\b",
    re.IGNORECASE,
)


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date, Decimal, UUID)):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return str(value)


def record_hash(payload: dict[str, Any]) -> str:
    """Return a stable digest independent of database column order."""
    canonical = json.dumps(
        _json_value(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _statement(input_data: dict[str, Any]) -> tuple[str, str]:
    query = input_data.get("sourceQuery") or input_data.get("source_query")
    table = input_data.get("sourceTable") or input_data.get("source_table")
    if query:
        if (
            not isinstance(query, str)
            or not READ_ONLY_QUERY.match(query)
            or not FROM_CLAUSE.search(query)
            or FORBIDDEN_SQL.search(query)
        ):
            raise ValueError("数据库查询必须是一条包含 FROM 的只读 SELECT 语句，例如 SELECT * FROM course")
        return query.strip(), "query"
    if not isinstance(table, str) or not TABLE_NAME.fullmatch(table.strip()):
        raise ValueError("请填写要接入的数据表，或提供只读 SELECT 查询")
    return f"SELECT * FROM {table.strip()}", "table"


def read_mysql_rows(input_data: dict[str, Any]) -> tuple[list[dict[str, Any]], int, str]:
    """从外部 MySQL 读取数据。连接串必须为 mysql+pymysql://...。"""
    address = input_data.get("sourceAddress") or input_data.get("source_address")
    if not isinstance(address, str) or not address.strip():
        raise ValueError("数据库连接地址不能为空")
    try:
        url = make_url(address.strip())
    except Exception as error:
        raise ValueError("数据库连接地址格式无效") from error
    if url.drivername != "mysql+pymysql":
        raise ValueError("数据库地址必须使用 mysql+pymysql:// 格式")

    statement, source_kind = _statement(input_data)
    engine = create_engine(address.strip(), pool_pre_ping=True, connect_args={"connect_timeout": 10})
    rows: list[dict[str, Any]] = []
    byte_count = 0
    try:
        with engine.connect() as connection:
            result = connection.execution_options(stream_results=True).execute(text(statement))
            while batch := result.mappings().fetchmany(FETCH_SIZE):
                if len(rows) + len(batch) > MAX_DATABASE_ROWS:
                    raise ValueError(f"单次数据库接入最多 {MAX_DATABASE_ROWS} 条记录")
                for row in batch:
                    payload = _json_value(dict(row))
                    rows.append(payload)
                    byte_count += len(str(payload).encode("utf-8"))
    except ValueError:
        raise
    except Exception as error:
        raise ValueError(f"MySQL 连接或读取失败：{error}") from error
    finally:
        engine.dispose()
    return rows, byte_count, source_kind


def save_dataset_rows(
    db: Session,
    *,
    dataset_id: int,
    task_id: str,
    rows: list[dict[str, Any]],
    content_hashes: list[str] | None = None,
) -> int:
    """Write every source row and return the number with an existing content hash.

    Duplicate data remains available to downstream users.  A duplicate is thus
    counted in both the successful-ingest total and the duplicate total.
    File imports supply hashes of the original bytes so upload metadata does
    not affect duplicate detection; database rows retain their JSON hashes.
    """
    if content_hashes is not None and len(content_hashes) != len(rows):
        raise ValueError("内容指纹数量与接入记录数量不一致")
    hashes = content_hashes if content_hashes is not None else [record_hash(row) for row in rows]
    existing_hashes = set(
        db.scalars(
            select(DatasetRecord.content_hash).where(
                DatasetRecord.dataset_id == dataset_id,
                DatasetRecord.content_hash.in_(set(hashes)),
            )
        ).all()
    ) if hashes else set()
    seen_hashes = set(existing_hashes)
    duplicate_count = 0
    for start in range(0, len(rows), FETCH_SIZE):
        batch = []
        for row, content_hash in zip(rows[start : start + FETCH_SIZE], hashes[start : start + FETCH_SIZE]):
            if content_hash in seen_hashes:
                duplicate_count += 1
            seen_hashes.add(content_hash)
            batch.append(DatasetRecord(
                dataset_id=dataset_id,
                task_id=task_id,
                content_hash=content_hash,
                payload=row,
            ))
        db.add_all(
            batch
        )
        db.flush()
    return duplicate_count
