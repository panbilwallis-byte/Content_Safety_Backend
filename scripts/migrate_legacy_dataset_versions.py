"""Explicit MySQL migration for the legacy JSON snapshot version table.

Stop all database writers first. Default is read-only; pass --apply to migrate.
Keeps legacy columns and creates timestamped SQL backup tables before DDL.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.core.database import engine
from app.models.tables import Dataset, DatasetRecord, DatasetVersion, DatasetVersionItem
from app.services.database_ingest import record_hash
from app.services.dataset_version_service import manifest_hash


def prepare(rows):
    versions = {}
    for row in rows:
        item = dict(row)
        samples = item['samples']
        if isinstance(samples, str):
            samples = json.loads(samples)
        if not isinstance(samples, list) or not all(isinstance(s, dict) for s in samples):
            raise ValueError('Legacy samples must be a list of objects')
        identities = [str(s.get('id', '')) for s in samples]
        if any(not identity for identity in identities) or len(set(identities)) != len(identities):
            raise ValueError('Missing or duplicate legacy sample IDs; manual review required')
        if any('dataset_id' in s and str(s['dataset_id']) != str(item['dataset_id']) for s in samples):
            raise ValueError('Sample belongs to another dataset')
        item['samples'] = samples
        key = (item['dataset_id'], item['version'])
        if key in versions:
            raise ValueError('Duplicate dataset/version')
        versions[key] = item
    ordered = []
    pending = dict(versions)
    while pending:
        ready = [key for key, row in pending.items()
                 if not row['source_version'] or
                 (row['dataset_id'], row['source_version']) in
                 {(r['dataset_id'], r['version']) for r in ordered}]
        if not ready:
            raise ValueError('Missing parent version or cycle; no schema changes made')
        for key in ready:
            ordered.append(pending.pop(key))
    return ordered


def convert(db, rows):
    """Preserve complete snapshot payloads; never guess record-1 means row 1."""
    version_ids = {r['id']: 'dsv_{:06d}'.format(r['id']) for r in rows}
    by_label = {(r['dataset_id'], r['version']): r for r in rows}
    sample_records = {}
    for row in rows:
        if db.get(Dataset, row['dataset_id']) is None:
            raise ValueError('Missing dataset')
        parent = by_label.get((row['dataset_id'], row['source_version']))
        version_id = version_ids[row['id']]
        pairs = []
        for sample in row['samples']:
            identity = str(sample['id'])
            parent_record = sample_records.get((parent['id'], identity)) if parent else None
            record = DatasetRecord(
                dataset_id=row['dataset_id'],
                task_id=row['task_id'] or 'legacy_snapshot_{}'.format(row['id']),
                payload=sample, content_hash=record_hash(sample),
                source_record_id=parent_record,
                created_at=row['created_at'],
            )
            db.add(record)
            db.flush()
            sample_records[(row['id'], identity)] = record.id
            pairs.append((record.id, record.content_hash))
            db.add(DatasetVersionItem(version_id=version_id, record_id=record.id,
                                      content_hash=record.content_hash))
        db.execute(text('''UPDATE dataset_versions SET version_id=:vid, label=:label,
            source_version_id=:parent, producing_task_id=:task,
            change_summary=:summary, sample_count=:count, manifest_hash=:digest
            WHERE id=:id'''), dict(
                vid=version_id, label=row['version'],
                parent=version_ids[parent['id']] if parent else None,
                task=row['task_id'], summary='旧版 JSON 快照迁移（保留原文）',
                count=len(pairs), digest=manifest_hash(pairs), id=row['id']))
    db.flush()
    for row in rows:
        vid = version_ids[row['id']]
        version = db.get(DatasetVersion, vid)
        items = db.scalars(select(DatasetVersionItem).where(
            DatasetVersionItem.version_id == vid).order_by(DatasetVersionItem.id)).all()
        payloads = [db.get(DatasetRecord, item.record_id).payload for item in items]
        if (payloads != row['samples'] or version.sample_count != len(items)
                or version.manifest_hash != manifest_hash((i.record_id, i.content_hash) for i in items)):
            raise ValueError('Snapshot verification failed; data transaction will roll back')
        print('{}: {} samples verified'.format(vid, len(items)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if engine.dialect.name != 'mysql':
        raise ValueError('This migration requires MySQL')
    inspector = inspect(engine)
    columns = {c['name']: c for c in inspector.get_columns('dataset_versions')}
    if not {'id', 'version', 'source_version', 'task_id', 'samples'} <= columns.keys():
        raise ValueError('Not the expected legacy schema; stop and review')
    with engine.connect() as conn:
        if 'version_id' in columns and conn.scalar(text(
                'SELECT COUNT(*) FROM dataset_versions WHERE version_id IS NOT NULL')):
            print('Version IDs already populated. No changes made; do not run migration twice.')
            return
        if conn.scalar(text('SELECT COUNT(*) FROM dataset_version_items')):
            raise ValueError('Version items must be empty for this migration')
        rows = prepare(conn.execute(text('SELECT * FROM dataset_versions ORDER BY id')).mappings().all())
        known = set(conn.scalars(text('SELECT id FROM datasets')).all())
        if any(row['dataset_id'] not in known for row in rows):
            raise ValueError('Missing dataset')
    print('Database:', engine.url.database)
    print('Legacy versions:', len(rows), 'snapshot samples:', sum(len(r['samples']) for r in rows))
    if not args.apply:
        print('Read-only check passed. Stop all writers, then rerun with --apply.')
        return
    # MySQL DDL commits implicitly. Keep physical backups before changing columns.
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    with engine.begin() as conn:
        for table in ('dataset_versions', 'dataset_version_items', 'dataset_records'):
            backup = '{}_bak_{}'.format(table, stamp)
            conn.execute(text('CREATE TABLE `{}` LIKE `{}`'.format(backup, table)))
            conn.execute(text('INSERT INTO `{}` SELECT * FROM `{}`'.format(backup, table)))
            print('Backup table:', backup, flush=True)
    additions = {
        'version_id': 'VARCHAR(64) NULL', 'label': 'VARCHAR(100) NULL',
        'source_version_id': 'VARCHAR(64) NULL', 'producing_task_id': 'VARCHAR(64) NULL',
        'change_summary': "VARCHAR(500) NOT NULL DEFAULT ''",
        'sample_count': 'INT NOT NULL DEFAULT 0',
        'manifest_hash': "VARCHAR(64) NOT NULL DEFAULT ''",
    }
    with engine.begin() as conn:
        for name, definition in additions.items():
            if name not in columns:
                conn.execute(text('ALTER TABLE dataset_versions ADD COLUMN `{}` {}'.format(name, definition)))
        # Keep old fields for evidence, but allow new ORM inserts to omit them.
        conn.execute(text('ALTER TABLE dataset_versions MODIFY COLUMN version VARCHAR(100) NULL, '
                          'MODIFY COLUMN samples JSON NULL'))
    with Session(engine) as db:
        with db.begin():
            convert(db, rows)
    indexes = inspect(engine).get_indexes('dataset_versions')
    with engine.begin() as conn:
        if not any(i.get('unique') and i['column_names'] == ['version_id'] for i in indexes):
            conn.execute(text('CREATE UNIQUE INDEX uq_migrated_version_id ON dataset_versions (version_id)'))
        conn.execute(text('ALTER TABLE dataset_versions MODIFY COLUMN version_id VARCHAR(64) NOT NULL, '
                          'MODIFY COLUMN label VARCHAR(100) NOT NULL'))
    print('Migration complete. Legacy columns and backup tables retained. Restart backend.')


if __name__ == '__main__':
    main()
