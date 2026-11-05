"""真实审计快照到评估/导出的隔离回归，不写业务数据库、不调用模型。"""
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4
import json

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from app.models.tables import (Base, Task, TrainingTask, Dataset, DatasetVersion, Model, ModelVersion,
                               EvaluationWorkspaceItem, EvaluationRun, EvaluationMetricRevision)
from app.core.time import now_shanghai
from app.routers import evaluations as api
from app.services import audit_evaluation as service


@pytest.fixture
def dbs(monkeypatch):
    engine = create_engine('sqlite+pysqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(api, 'SessionLocal', sessions)
    with sessions() as db:
        db.add(Task(task_id='task-real', name='已执行任务', capability_code='data_ingest', status='succeeded',
                    input_data={'dataset_id': 1, 'endpoint': '/ingest'}, config={}, result={'output': '已接入'}, dataset_version='v1',
                    created_at=now_shanghai(), finished_at=now_shanghai()))
        db.commit()
    yield sessions
    engine.dispose()


def save(dbs, kind='task', entity_id='task-real', **kw):
    body = api.AuditSnapshotRequest(kind=kind, entity_type={'task': 'task', 'checkpoints': 'training_task', 'lineage': 'model'}[kind], entity_id=entity_id, **kw)
    request = Request({'type': 'http', 'headers': [], 'state': {'user': {'uid': 1}}})
    return api.create_audit_snapshot(body, request)['data']


def run(dbs, source):
    with dbs() as db:
        revisions = list(db.scalars(select(EvaluationMetricRevision.revision_id).where(EvaluationMetricRevision.metric_code.in_(source['supportedMetricCodes']))))
    c = {'targetStage': 'final', 'executionMode': 'reference', 'sourceRefs': [source],
         'datasetVersionRef': source['datasetVersion'], 'labelVersionRef': source['labelVersion'],
         'modelVersionRef': source['modelVersion'], 'metricRevisionRefs': revisions, 'sampleScope': {'mode': 'all'}}
    pre = api.preflight(c)['data']
    assert pre['canCreate'], pre['issues']
    c['preflightToken'] = pre['token']
    task = api.create_task(api.CreateTask(name='审计评估', config={'evaluation': c}), Request({'type': 'http', 'headers': []}))['data']
    api.start(task['taskId'])
    return task['runId'], c


def test_task_end_to_end_and_immutable_source(dbs):
    source = save(dbs)
    assert len(source['versionId']) == 64
    with dbs() as db:
        before = service.freeze(db, source)
        db.get(Task, 'task-real').result = {}
        db.commit()
        assert service.freeze(db, source) == before
    rid, _ = run(dbs, source)
    with dbs() as db:
        row = db.get(EvaluationRun, rid)
        assert row.judgment_status == 'passed'
        assert row.metric_results[0]['numerator'] == row.metric_results[0]['denominator'] == 5
    details = api.samples(rid, source['supportedMetricCodes'][0])['data']
    assert len(details['items']) == 5
    assert all(s['includedInMetric'] for s in details['items'])
    proof = api.evidence(rid)['data']
    assert proof['integrityState'] == 'complete'
    eid = proof['entries'][0]['evidenceId']
    assert api.evidence_detail(eid)['data'] == proof['entries'][0]
    export = api.create_export(rid, {})['data']
    artifact = api.download_artifact(export['artifactId'])['data']
    assert sha256(artifact['content'].encode()).hexdigest() == api.download_ticket(export['artifactId'])['data']['sha256']
    assert json.loads(artifact['content'])['audit'] == before


def test_missing_field_not_pass_and_old_snapshot_stays_fixed(dbs):
    with dbs() as db:
        db.get(Task, 'task-real').input_data = {'dataset_id': 1}
        db.commit()
    old = save(dbs)
    rid, _ = run(dbs, old)
    with dbs() as db:
        row = db.get(EvaluationRun, rid)
        assert row.judgment_status == 'failed'
        assert row.metric_results[0]['value'] == .8
        db.get(Task, 'task-real').input_data = {'dataset_id': 1, 'endpoint': '/fixed'}
        db.commit()
    new = save(dbs)
    assert old['entityId'] != new['entityId'] and old['versionId'] != new['versionId']
    with dbs() as db:
        assert service.freeze(db, old)['samples'][2]['correct'] is False


@pytest.mark.parametrize('case', ['wrong_version', 'corrupt'])
def test_source_mismatch_and_tamper_rejected(dbs, case):
    source = save(dbs)
    with dbs() as db:
        if case == 'wrong_version':
            source['versionId'] = '0' * 64
        else:
            row = db.get(EvaluationWorkspaceItem, source['entityId'])
            p = deepcopy(row.payload)
            p['block']['samples'][0]['correct'] = False
            row.payload = p
            db.commit()
        with pytest.raises(HTTPException):
            service.resolve_source(db, source)


def test_execution_tampering_prevents_export(dbs):
    source = save(dbs)
    rid, _ = run(dbs, source)
    with dbs() as db:
        row = db.get(EvaluationRun, rid)
        row.metric_results = [{**row.metric_results[0], 'numerator': 99}]
        db.commit()
    assert api.evidence(rid)['data']['integrityState'] == 'corrupt'
    with pytest.raises(HTTPException):
        api.create_export(rid, {})


def test_simulated_and_missing_sources_rejected(dbs):
    with dbs() as db:
        db.get(Task, 'task-real').result = {'is_mock': True}
        db.commit()
    with pytest.raises(HTTPException):
        save(dbs)
    with pytest.raises(HTTPException):
        save(dbs, entity_id='missing')


def test_listing_does_not_write(dbs):
    assert api.contexts('audit_record')['data']['candidates'] == []
    with dbs() as db:
        assert db.scalar(select(func.count()).select_from(EvaluationWorkspaceItem)) == 0


def test_preflight_rejects_changed_rules_rerun_and_wrong_metric(dbs):
    source = save(dbs)
    _, config = run(dbs, source)
    for field, value in [('executionMode', 'rerun'), ('labelVersionRef', 'wrong'), ('sampleScope', {'mode': 'partial'})]:
        assert not api.preflight({**config, field: value})['data']['canCreate']
    with dbs() as db:
        service.ensure_metrics(db)
        db.commit()
        revision = db.scalar(select(EvaluationMetricRevision.revision_id).where(EvaluationMetricRevision.metric_code == service.CODES['lineage']))
    assert not api.preflight({**config, 'metricRevisionRefs': [revision]})['data']['canCreate']


def training(dbs):
    sid = str(uuid4())
    with dbs() as db:
        db.add(TrainingTask(id=sid, name='真实训练', model_id='1', base_version='v0', dataset_id='1',
            dataset_version='v1', target_version='v1', epochs=3, epoch=3, learning_rate=.01,
            batch_size=2, method='脚本', status='succeeded', checkpoints=[{'epoch': 1}, {'epoch': 3}]))
        db.commit()
    return sid


def test_checkpoint_expected_schedule_independent_and_unknown(dbs, monkeypatch):
    sid = training(dbs)
    monkeypatch.setattr(service, 'training_artifacts', lambda _: [{'id': 'a', 'filename': 'outputs/last.pt', 'epoch': None, 'verified': True}])
    source = save(dbs, 'checkpoints', sid)
    with dbs() as db:
        block = service.freeze(db, source)
        assert block['material']['expectedEpochs'] == [1, 2, 3]
        assert [s['predictionStatus'] for s in block['samples']] == ['unknown', 'missing', 'verified']
    rid, _ = run(dbs, source)
    with dbs() as db:
        assert db.get(EvaluationRun, rid).judgment_status == 'inconclusive'


def test_lineage_missing_stages_not_inferred_from_actual_graph(dbs):
    with dbs() as db:
        db.add(Model(id=1, name='模型', version='v1'))
        db.add(ModelVersion(model_id=1, version='v1'))
        db.commit()
    source = save(dbs, 'lineage', '1', version_id='v1')
    rid, _ = run(dbs, source)
    with dbs() as db:
        block = service.freeze(db, source)
        assert [s['predictionStatus'] for s in block['samples']] == ['missing', 'missing', 'missing']
        assert db.get(EvaluationRun, rid).judgment_status == 'failed'
    with pytest.raises(HTTPException):
        save(dbs, 'lineage', '1', version_id='v99')


def test_authentication_and_reader_write_block(dbs, monkeypatch):
    from app.main import enforce_platform_auth
    from app.core.auth import create_token
    app = FastAPI()
    app.middleware('http')(enforce_platform_auth)
    app.include_router(api.router, prefix='/api/v1')
    client = TestClient(app)
    path = '/api/v1/evaluation/audit-snapshots'
    body = {'kind': 'task', 'entity_type': 'task', 'entity_id': 'task-real'}
    assert client.post(path, json=body).status_code == 401
    reader = create_token(2, 'reader', '审计员')
    assert client.post(path, json=body, headers={'Authorization': 'Bearer ' + reader}).status_code == 403
    operator = create_token(1, 'admin', '超级管理员')
    response = client.post(path, json=body, headers={'Authorization': 'Bearer ' + operator})
    assert response.status_code == 200
    source = response.json()['data']
    url = path + '/' + source['entityId']
    assert client.get(url, params={'version_id': source['versionId']}).status_code == 401
    assert client.get(url, params={'version_id': source['versionId']}, headers={'Authorization': 'Bearer ' + reader}).status_code == 200


def test_lineage_base_version_is_not_a_training_output(dbs):
    sid = training(dbs)
    with dbs() as db:
        db.add(Dataset(id=1, name='训练集', version='v1'))
        db.add(DatasetVersion(version_id='dsv1', dataset_id=1, label='v1', sample_count=0, manifest_hash='a' * 64))
        db.add(Model(id=1, name='模型', version='v0'))
        db.add(ModelVersion(model_id=1, version='v0'))
        db.commit()
    source = save(dbs, 'lineage', '1', version_id='v0')
    with dbs() as db:
        rows = service.freeze(db, source)['samples']
        assert [r['predictionStatus'] for r in rows[:3]] == ['verified', 'verified', 'missing']
        db.add(ModelVersion(model_id=1, version='v1', task_id=sid))
        db.commit()
    newer = save(dbs, 'lineage', '1', version_id='v1')
    with dbs() as db:
        assert service.freeze(db, newer)['samples'][2]['predictionStatus'] == 'verified'
        assert service.freeze(db, source)['samples'][2]['predictionStatus'] == 'missing'


@pytest.mark.parametrize('corrupt', [False, True])
def test_weight_download_verifies_real_bytes(monkeypatch, corrupt):
    raw = b'checkpoint-bytes'
    expected = sha256(raw).hexdigest()
    def respond(request):
        if request.url.path.endswith('/artifacts'):
            return httpx.Response(200, json={'code': 0, 'data': [{'id': 'a', 'filename': 'last.pt', 'size': len(raw), 'sha256': expected}]})
        return httpx.Response(200, content=b'tampered' if corrupt else raw)
    client_type = httpx.Client
    monkeypatch.setenv('LLM_ACCESS_TOKEN', 'unit-test')
    monkeypatch.setattr(service.httpx, 'Client', lambda **kw: client_type(transport=httpx.MockTransport(respond), **kw))
    assert service.training_artifacts('task')[0]['verified'] is (not corrupt)


def test_empty_frozen_checks_are_inconclusive(dbs):
    source = save(dbs)
    rid, _ = run(dbs, source)
    with dbs() as db:
        row = db.get(EvaluationRun, rid)
        snapshot = deepcopy(row.snapshot)
        snapshot['auditEvaluation']['samples'] = []
        snapshot['auditEvaluationHash'] = service.digest(snapshot['auditEvaluation'])
        row.snapshot = snapshot
        service.calculate(row)
        assert row.judgment_status == 'inconclusive'
        assert row.metric_results[0]['reasonCode'] == 'zero_denominator'
