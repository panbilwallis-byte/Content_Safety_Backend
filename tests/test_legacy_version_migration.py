import json
from datetime import datetime

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from app.models.tables import Base, Dataset, DatasetRecord, DatasetVersion
from scripts.migrate_legacy_dataset_versions import convert, prepare
from app.services.dataset_version_service import register_version


def test_missing_parent_and_duplicate_sample_ids_rejected():
    row = dict(id=1, dataset_id=3, version='v2', source_version='v1',
               task_id=None, samples=[{'id': 'record-1'}])
    with pytest.raises(ValueError, match='Missing parent'):
        prepare([row])
    with pytest.raises(ValueError, match='duplicate'):
        prepare([{**row, 'source_version': None, 'samples': [{'id': 'x'}, {'id': 'x'}]}])


def test_preserves_distinct_snapshot_text_parent_links_and_future_inserts():
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for definition in ('id INTEGER', 'version VARCHAR(100)',
                           'source_version VARCHAR(100)', 'task_id VARCHAR(64)', 'samples JSON'):
            conn.execute(text('ALTER TABLE dataset_versions ADD COLUMN ' + definition))
    now = datetime(2026, 11, 18)
    raw = [dict(id=1, dataset_id=3, version='v1', source_version=None,
                task_id=None, samples=[{'id': 'record-1', 'text': 'original'}], created_at=now),
           dict(id=2, dataset_id=3, version='v2', source_version='v1',
                task_id='process-1', samples=[{'id': 'record-1', 'text': 'changed'}], created_at=now)]
    rows = prepare(list(reversed(raw)))
    with Session(engine) as db, db.begin():
        dataset = Dataset(id=3, name='test', version='v2', source_type='business')
        db.add(dataset)
        original = DatasetRecord(id=1, dataset_id=3, task_id='original',
                                 payload={'text': 'unrelated existing row'})
        db.add(original)
        db.flush()
        for row in rows:
            db.execute(text('''INSERT INTO dataset_versions
                (id, version, samples, dataset_id, created_at, version_id, label,
                 change_summary, sample_count, manifest_hash)
                VALUES (:id, :version, :samples, 3, :created, :temporary, '', '', 0, '')'''),
                dict(id=row['id'], version=row['version'], samples=json.dumps(row['samples']),
                     created=now, temporary='temporary-{}'.format(row['id'])))
        convert(db, rows)
        records = db.scalars(select(DatasetRecord).order_by(DatasetRecord.id)).all()
        assert records[0].payload == {'text': 'unrelated existing row'}
        assert records[1].payload == rows[0]['samples'][0]
        assert records[2].payload == rows[1]['samples'][0]
        assert records[2].source_record_id == records[1].id
        assert db.get(DatasetVersion, 'dsv_000002').source_version_id == 'dsv_000001'
        new = register_version(db, dataset, 'v3', [records[2].id])
        assert new.version_id == 'dsv_000003'
