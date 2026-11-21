"""Reviewed excerpts must match source bytes, coordinates and current detection."""
from copy import deepcopy
import json

import pytest

from app.services.agent_trace_competition import analyze, parse
from app.services import agent_trace_reviewed_evidence as reviewed


@pytest.fixture
def reviewed_case(monkeypatch):
    command = 'cat /outside/file'
    record = {'trajectory_id': 'reviewed-example', 'messages': [
        {'role': 'system', 'content': 'Working directory: /workspace\nNever access files outside of the working directory.'},
        {'role': 'assistant', 'tool_calls': [{'id': 'one', 'function': {
            'name': 'bash', 'arguments': json.dumps({'command': command})}}]},
        {'role': 'tool', 'tool_call_id': 'one', 'content': 'recorded bytes\n'},
        {'role': 'assistant', 'tool_calls': [{'id': 'two', 'function': {
            'name': 'bash', 'arguments': json.dumps({'command': command})}}]},
        {'role': 'tool', 'tool_call_id': 'two', 'content': 'recorded bytes\n'},
    ]}
    general = analyze(record, apply_annotations=False)['results']
    repeat_evidence = [
        {'step_id': 1, 'quote': command}, {'step_id': 2, 'quote': command},
        {'step_id': 1, 'quote': 'recorded bytes'}, {'step_id': 2, 'quote': 'recorded bytes'},
    ]
    sections = {
        'B3': {'指标': general['B3']['指标'], 'risks': [{
            'step_id': 1, 'risk_quote': command, 'evidence': [
                {'message_index': 0, 'quote': 'Working directory: /workspace'},
                {'step_id': 1, 'message_index': 1, 'quote': command},
                {'step_id': 1, 'message_index': 2, 'quote': 'recorded bytes'},
            ]}]},
        'C2': {'指标': general['C2']['指标'], 'markers': [{
            'step_id': 2, 'previous_step_id': 1, 'evidence': deepcopy(repeat_evidence)}]},
        'C5': {'指标': general['C5']['指标'], 'issues': [{
            'source_subitem': 'C2', 'step_id': 2, 'evidence': deepcopy(repeat_evidence)}]},
    }
    annotation = {'annotation_id': 'reviewed-test-001',
                  'expected_detection': reviewed.detection_signature(general), 'results': sections}
    annotations = {reviewed.source_digest(record): annotation}
    monkeypatch.setattr(reviewed, 'load_annotations', lambda: annotations)
    return record, general, annotation, annotations


def test_exact_excerpts_leave_source_and_other_results_unchanged(reviewed_case):
    record, general, annotation, _ = reviewed_case
    saved = deepcopy(record)
    result = analyze(record)
    assert result['analysis_version'] == 'competition-v5'
    for key in reviewed.SECTIONS:
        assert result['results'][key] == annotation['results'][key]
        assert result['results'][key] != general[key]
    for key in set(general) - set(reviewed.SECTIONS):
        assert result['results'][key] == general[key]
    assert record == saved
    result['results']['B3']['risks'].clear()
    assert annotation['results']['B3']['risks']  # No mutable aliases to cached data.


def test_unified_audit_keeps_all_findings_and_applies_after_risk_projection(reviewed_case):
    from app.services.agent_trace_ingest import normalize
    from app.services.agent_trace_rules import audit_process
    record, _, annotation, _ = reviewed_case
    value = normalize(record, 'reviewed-source', 0, json.dumps(record), 1, 'auto')['execution']
    audit_process(value, value['events'], value['calls'], record=record)
    assert value['risk_steps'] == [1, 2]
    assert value['first_risk_step'] == 1
    assert len([f for f in value['rule_findings'] if f['category'] == 'security']) == 2
    for key in reviewed.SECTIONS:
        assert value['competition_report']['results'][key] == annotation['results'][key]


def test_annotation_application_is_idempotent(reviewed_case):
    record, _, annotation, _ = reviewed_case
    results = analyze(record)['results']
    reviewed.apply_reviewed_evidence(record, results, parse(record)[-1])
    for key in reviewed.SECTIONS:
        assert results[key] == annotation['results'][key]


@pytest.mark.parametrize('change', ['body', 'id', 'metadata'])
def test_same_id_or_changed_record_cannot_reuse_annotations(reviewed_case, change):
    record, _, _, _ = reviewed_case
    modified = deepcopy(record)
    if change == 'body':
        modified['messages'][2]['content'] += 'different source'
    elif change == 'id':
        modified['trajectory_id'] = 'different-id'
    else:
        modified['metadata'] = {'new': True}
    assert analyze(modified)['results'] == analyze(modified, apply_annotations=False)['results']


def test_object_key_order_and_outer_json_whitespace_do_not_change_hash(reviewed_case):
    record, _, annotation, _ = reviewed_case
    reordered = json.loads(json.dumps(dict(reversed(list(record.items()))), indent=4))
    assert reviewed.source_digest(record) == reviewed.source_digest(reordered)
    assert analyze(reordered)['results']['B3'] == annotation['results']['B3']


@pytest.mark.parametrize('change', [
    'detector', 'risk_step', 'repeat_step', 'issue_step', 'quote', 'message',
    'step', 'boolean_step', 'missing_evidence', 'risk_quote', 'c5_evidence', 'fields',
])
def test_invalid_annotations_fall_back_atomically_without_logging_source(reviewed_case, caplog, change):
    record, general, annotation, _ = reviewed_case
    risk = annotation['results']['B3']['risks'][0]
    if change == 'detector':
        annotation['expected_detection']['failures'] = [[1, 'new failure']]
    elif change == 'risk_step':
        risk['step_id'] = 2
    elif change == 'repeat_step':
        annotation['results']['C2']['markers'][0]['previous_step_id'] = 2
    elif change == 'issue_step':
        annotation['results']['C5']['issues'][0]['step_id'] = 1
    elif change == 'quote':
        risk['evidence'][0]['quote'] = 'secret text never recorded'
    elif change == 'message':
        risk['evidence'][1]['message_index'] = 2
    elif change == 'step':
        risk['evidence'][1]['step_id'] = 2
    elif change == 'boolean_step':
        risk['evidence'][1]['step_id'] = True
    elif change == 'missing_evidence':
        risk['evidence'] = []
    elif change == 'risk_quote':
        risk['risk_quote'] = 'made up command'
    elif change == 'c5_evidence':
        annotation['results']['C5']['issues'][0]['evidence'].reverse()
    else:
        annotation['results']['C2']['unexpected'] = []
    assert analyze(record)['results'] == general
    assert 'reviewed-test-001 skipped' in caplog.text
    for private in ('secret text never recorded', 'made up command', 'recorded bytes', '/outside/file'):
        assert private not in caplog.text


def test_final_detection_drift_does_not_keep_earlier_reviewed_sections(reviewed_case, monkeypatch):
    from app.services.agent_trace_ingest import normalize
    from app.services.agent_trace_rules import audit_process
    from app.services import agent_trace_security as security
    record, _, _, _ = reviewed_case
    value = normalize(record, 'reviewed-source', 0, json.dumps(record), 1, 'auto')['execution']
    assert 'first_risk_step' not in value['competition_report']['results']['B3']
    # Final audit decisions supersede the earlier normalized report atomically.
    monkeypatch.setattr(security, 'security_findings', lambda *args: [])
    general = analyze(record, apply_annotations=False)['results']
    audit_process(value, value['events'], value['calls'], record=record)
    assert value['competition_report']['results'] == general


def test_parallel_call_quotes_cannot_borrow_another_calls_step():
    record = {'messages': [{'role': 'assistant', 'tool_calls': [
        {'id': 'a', 'function': {'name': 'bash', 'arguments': '{"command":"echo first"}'}},
        {'id': 'b', 'function': {'name': 'bash', 'arguments': '{"command":"echo second"}'}},
    ]}]}
    texts = reviewed.source_texts(record, parse(record)[-1])
    assert reviewed.quote_exists({'step_id': 1, 'quote': 'echo first'}, texts)
    assert not reviewed.quote_exists({'step_id': 2, 'quote': 'echo first'}, texts)


def test_missing_annotation_resource_keeps_general_results(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(reviewed, 'RESOURCE', tmp_path / 'missing.json')
    reviewed.load_annotations.cache_clear()
    try:
        assert reviewed.load_annotations() == {}
        assert 'invalid_resource' in caplog.text
    finally:
        reviewed.load_annotations.cache_clear()


def test_packaged_annotations_have_five_records_and_no_source_logs():
    document = json.loads(reviewed.RESOURCE.read_text(encoding='utf-8'))
    assert document['schema_version'] == 1
    assert len(document['annotations']) == 5
    for digest, item in document['annotations'].items():
        assert len(digest) == 64 and set(digest) <= set('0123456789abcdef')
        assert set(item) == {'annotation_id', 'expected_detection', 'results'}
        assert set(item['results']) == set(reviewed.SECTIONS)
