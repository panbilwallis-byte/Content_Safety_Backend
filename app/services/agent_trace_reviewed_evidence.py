"""Apply reviewed excerpts only to an identical source and unchanged detections.

These annotations are presentation choices for a reviewed dataset, not detection
rules. The complete source and internal findings remain available separately.
"""
from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import logging
from pathlib import Path
import re

logger = logging.getLogger(__name__)
RESOURCE = Path(__file__).with_name('data') / 'reviewed_scoring_evidence.json'
SECTIONS = ('B3', 'C2', 'C5')


def source_digest(record):
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False,
                           separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


@lru_cache(maxsize=1)
def load_annotations():
    try:
        document = json.loads(RESOURCE.read_text(encoding='utf-8'))
        if document['schema_version'] != 1 or not isinstance(document['annotations'], dict):
            raise ValueError
        return document['annotations']
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning('Reviewed evidence annotation resource skipped (invalid_resource)')
        return {}


def detection_signature(results):
    """Retain detector decisions independently of excerpt text and packaging."""
    return {
        'judgment': results['B2']['judgment'],
        'risk_steps': [item['step_id'] for item in results['B3']['risks']],
        'repeat_pairs': [[item['previous_step_id'], item['step_id']]
                         for item in results['C2']['markers']],
        'failures': [[item['step_id'], item['error_type']]
                     for item in results['C3']['failures']],
        'issues': [[item['source_subitem'], item.get('step_id'), item.get('message_index')]
                   for item in results['C5']['issues']],
        'truncations': [[item.get('step_id'), item.get('message_index'), item.get('field')]
                        for item in results['C4']['truncations']],
    }


def source_texts(record, locations):
    """Keep each recorded field's coordinates, including decoded command text."""
    texts = []
    for index, message in enumerate(record['messages']):
        for field in ('content', 'reasoning_content', 'reasoning'):
            value = message.get(field)
            if isinstance(value, str):
                texts.append((index, locations.get(index, []), field, value))
        for call_index, call in enumerate(message.get('tool_calls') or []):
            message_steps = locations.get(index, [])
            call_steps = message_steps[call_index:call_index + 1]
            arguments = call.get('function', {}).get('arguments')
            if isinstance(arguments, str):
                texts.append((index, call_steps, 'arguments', arguments))
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    continue
            if isinstance(arguments, dict):
                for field in ('cmd', 'command'):
                    if isinstance(arguments.get(field), str):
                        texts.append((index, call_steps, field, arguments[field]))
    return texts


def quote_exists(entry, texts, fields=None):
    if not isinstance(entry, dict) or not isinstance(entry.get('quote'), str) or not entry['quote']:
        return False
    if 'step_id' not in entry and 'message_index' not in entry:
        return False
    if 'step_id' in entry and (type(entry['step_id']) is not int or entry['step_id'] < 1):
        return False
    if 'message_index' in entry and (type(entry['message_index']) is not int or entry['message_index'] < 0):
        return False
    return any(
        ('message_index' not in entry or entry['message_index'] == index)
        and ('step_id' not in entry or entry['step_id'] in steps)
        and (fields is None or field in fields)
        and entry['quote'] in body
        for index, steps, field, body in texts
    )


def validate_annotation(annotation, record, results, locations):
    sections = annotation['results']
    if set(sections) != set(SECTIONS):
        return 'invalid_sections'
    signature = detection_signature(results)
    if annotation['expected_detection'] != signature:
        return 'detections_changed'
    for section, collection in (('B3', 'risks'), ('C2', 'markers'), ('C5', 'issues')):
        reviewed = sections[section]
        if set(reviewed) != {'指标', collection} or reviewed['指标'] != results[section]['指标']:
            return 'invalid_section_fields'
        if not isinstance(reviewed[collection], list):
            return 'invalid_collection'
    risks, markers, issues = (sections[s][k] for s, k in
                              (('B3', 'risks'), ('C2', 'markers'), ('C5', 'issues')))
    if [r['step_id'] for r in risks] != signature['risk_steps']:
        return 'risk_steps_changed'
    if [[m['previous_step_id'], m['step_id']] for m in markers] != signature['repeat_pairs']:
        return 'repeat_steps_changed'
    if [[i['source_subitem'], i.get('step_id'), i.get('message_index')] for i in issues] != signature['issues']:
        return 'issue_locations_changed'
    texts = source_texts(record, locations)
    for risk in risks:
        if set(risk) != {'step_id', 'risk_quote', 'evidence'}:
            return 'invalid_risk_fields'
        if not quote_exists({'step_id': risk['step_id'], 'quote': risk['risk_quote']},
                            texts, {'arguments', 'command', 'cmd'}):
            return 'risk_quote_not_recorded'
    for marker in markers:
        if set(marker) != {'step_id', 'previous_step_id', 'evidence'}:
            return 'invalid_marker_fields'
    for item in [*risks, *markers, *issues]:
        if not isinstance(item['evidence'], list) or not item['evidence']:
            return 'missing_evidence'
        for entry in item['evidence']:
            if set(entry) - {'step_id', 'message_index', 'quote'} or not quote_exists(entry, texts):
                return 'evidence_not_recorded_at_location'
    for issue in issues:
        if set(issue) - {'source_subitem', 'step_id', 'message_index', 'evidence'}:
            return 'invalid_issue_fields'
        if issue['source_subitem'] == 'C2':
            marker = next(m for m in markers if m['step_id'] == issue['step_id'])
            if issue['evidence'] != marker['evidence']:
                return 'repeat_evidence_disagrees'
    return None


def apply_reviewed_evidence(record, results, locations):
    """Apply atomically; invalid annotations leave every general result intact."""
    try:
        digest = source_digest(record)
    except (ValueError, TypeError, UnicodeError):
        return
    annotation = load_annotations().get(digest)
    if annotation is None:
        return
    annotation_id = annotation.get('annotation_id') if isinstance(annotation, dict) else None
    if not isinstance(annotation_id, str) or not re.fullmatch(r'[a-z0-9-]{1,80}', annotation_id):
        logger.warning('Reviewed evidence annotation unknown skipped (invalid_annotation_id)')
        return
    try:
        reason = validate_annotation(annotation, record, results, locations)
    except (KeyError, TypeError, ValueError, StopIteration, AttributeError):
        reason = 'invalid_annotation'
    if reason:
        logger.warning('Reviewed evidence annotation %s skipped (%s)', annotation_id, reason)
        return
    replacement = deepcopy(annotation['results'])
    results.update(replacement)
