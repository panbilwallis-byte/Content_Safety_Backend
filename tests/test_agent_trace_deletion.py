"""Deletion contracts use isolated databases and temporary uploads only."""
from uuid import uuid4

import pytest
from sqlalchemy import select, func

from test_agent_traces import env, ROOT
from test_agent_trace_pages import pages, create, upload, parse, OPENAI
from app.models.agent_traces import (
    TraceBatch, TraceBinding, TraceAsset, TraceSnapshot, TraceProjection,
    TraceSample, TraceEvent, TraceCall, TraceRelation, TraceEvidence, TraceStatisticsSample,
)
from app.models.tables import Task
from app.core.auth import create_token


def remove(pages, path, version, key=None):
    return pages['client'].request('DELETE', ROOT + path,
        json={'expected_version': version}, headers={'X-Request-Id': key or uuid4().hex})


def bind(pages, batch, fid):
    r = pages['client'].post(ROOT + f"/batches/{batch['batch_id']}/files",
        json={'expected_version': batch['batch_version'], 'files': [{'file_id': fid}]},
        headers={'X-Request-Id': uuid4().hex})
    assert r.status_code == 200, r.text
    return r.json()['data']['batch']


def test_delete_empty_batch_and_retry(pages):
    batch = create(pages)
    path = f"/batches/{batch['batch_id']}"
    key = uuid4().hex
    first = remove(pages, path, 1, key)
    assert first.status_code == 200, first.text
    assert remove(pages, path, 1, key).json()['data'] == first.json()['data']
    assert pages['client'].get(ROOT + '/workspace', params={'batch_id': batch['batch_id']}).status_code == 404
    options = pages['client'].get(ROOT + '/workspace').json()['data']['batch_options']['items']
    assert batch['batch_id'] not in [b['batch_id'] for b in options]


def test_remove_pending_file_preserves_other_binding_and_shared_asset(pages):
    import json
    first, second = create(pages), create(pages)
    fid = upload(pages, json.dumps(OPENAI))
    first, second = bind(pages, first, fid), bind(pages, second, fid)
    path = f"/batches/{first['batch_id']}/files/{fid}"
    assert remove(pages, path, 1).status_code == 409  # stale version
    key = uuid4().hex
    result = remove(pages, path, first['batch_version'], key)
    assert result.status_code == 200, result.text
    assert remove(pages, path, first['batch_version'], key).status_code == 200
    with pages['sessions']() as db:
        assert db.get(TraceAsset, fid) is not None
        assert db.scalar(select(TraceBinding).where(TraceBinding.batch_id == first['batch_id'])) is None
        assert db.scalar(select(TraceBinding).where(TraceBinding.batch_id == second['batch_id'])) is not None
    workspace = pages['client'].get(ROOT + '/workspace', params={'batch_id': first['batch_id']}).json()['data']
    assert workspace['files']['total'] == 0
    assert workspace['batch']['batch_version'] == first['batch_version'] + 1
    assert not workspace['batch']['staging_changed']


def test_parsed_file_and_nonempty_batch_are_protected(pages):
    import json
    batch = create(pages)
    fid = upload(pages, json.dumps(OPENAI))
    parse(pages, batch, [fid])
    path = f"/batches/{batch['batch_id']}"
    current = pages['client'].get(ROOT + '/workspace', params={'batch_id': batch['batch_id']}).json()['data']['batch']
    assert remove(pages, path + f'/files/{fid}', current['batch_version']).status_code == 409
    with pages['sessions']() as db:
        snapshot_ids = list(db.scalars(select(TraceSnapshot.snapshot_id).where(TraceSnapshot.batch_id == batch['batch_id'])))
        assert snapshot_ids
    result = remove(pages, path, current['batch_version'])
    assert result.status_code == 409, result.text
    assert '还有文件' in result.json()['message']
    with pages['sessions']() as db:
        assert db.get(TraceBatch, batch['batch_id']) is not None
        assert db.get(TraceBatch, pages['batch']) is not None
        for model in (TraceSnapshot, TraceProjection, TraceSample, TraceEvent, TraceCall,
                      TraceRelation, TraceEvidence, TraceStatisticsSample):
            if model in (TraceSnapshot, TraceSample, TraceEvent, TraceCall):
                assert db.scalar(select(func.count()).select_from(model).where(model.snapshot_id.in_(snapshot_ids))) > 0
        assert db.get(TraceAsset, fid) is not None


def test_pending_file_blocks_batch_deletion_until_removed(pages):
    import json
    batch = create(pages)
    fid = upload(pages, json.dumps(OPENAI))
    batch = bind(pages, batch, fid)
    path = f"/batches/{batch['batch_id']}"
    result = remove(pages, path, batch['batch_version'])
    assert result.status_code == 409
    assert '还有文件' in result.json()['message']
    result = remove(pages, path + f'/files/{fid}', batch['batch_version'])
    assert result.status_code == 200, result.text
    version = result.json()['data']['batch']['batch_version']
    assert remove(pages, path, version).status_code == 200


@pytest.mark.parametrize('capability', ['agent_trace_ingest', 'agent_trace_export'])
def test_active_job_blocks_both_deletions(pages, capability):
    import json
    batch = create(pages)
    fid = upload(pages, json.dumps(OPENAI))
    batch = bind(pages, batch, fid)
    with pages['sessions']() as db:
        db.add(Task(task_id=uuid4().hex, name='pending test', capability_code=capability,
            status='pending', progress=0, input_data={'batch_id': batch['batch_id']}, config={}, result={}))
        db.commit()
    path = f"/batches/{batch['batch_id']}"
    assert remove(pages, path, batch['batch_version']).status_code == 409
    assert remove(pages, path + f'/files/{fid}', batch['batch_version']).status_code == 409


def test_readonly_and_internal_batches_protected(pages):
    batch = create(pages)
    pages['client'].headers['Authorization'] = 'Bearer ' + create_token(3, 'user-3', '审计员')
    assert remove(pages, f"/batches/{batch['batch_id']}", 1).status_code == 403
    pages['client'].headers['Authorization'] = 'Bearer ' + create_token(1, 'user-1', '管理员')
    assert remove(pages, f"/batches/{pages['batch']}", 1).status_code == 409
