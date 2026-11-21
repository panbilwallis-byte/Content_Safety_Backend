import re

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.tables import Dataset, DatasetRecord, DatasetVersion, DatasetVersionItem
from app.services.dataset_version_service import resolve_version


class GovernanceVersionScope:
    def __init__(self, db: Session, scope: dict, owner: str, *, lock: bool = False):
        query = select(Dataset).where(Dataset.id == scope["dataset_id"])
        self.dataset = db.scalar(query.with_for_update() if lock else query)
        if self.dataset is None:
            raise HTTPException(404, "数据集不存在")
        self.version = resolve_version(db, self.dataset.id, scope["version_id"])
        if self.version is None:
            raise HTTPException(404, "数据集版本不存在")
        self.scheme = scope["scheme_id"]
        self.language = scope["language"]
        self.owner = owner

    @property
    def is_current(self):
        return self.dataset.version == self.version.label


def version_records(scope: GovernanceVersionScope):
    return select(DatasetRecord).join(
        DatasetVersionItem, DatasetVersionItem.record_id == DatasetRecord.id,
    ).where(
        DatasetVersionItem.version_id == scope.version.version_id,
        DatasetRecord.dataset_id == scope.dataset.id,
    )


def member(db: Session, scope: GovernanceVersionScope, record_id: int):
    return db.scalar(version_records(scope).where(DatasetRecord.id == record_id))


def next_label(db: Session, scope: GovernanceVersionScope):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", scope.version.label)
    major, minor, patch = map(int, match.groups()) if match else (1, 0, 0)
    existing = set(db.scalars(select(DatasetVersion.label).where(
        DatasetVersion.dataset_id == scope.dataset.id,
    )).all())
    while True:
        patch += 1
        label = f"v{major}.{minor}.{patch}"
        if label not in existing:
            return label
