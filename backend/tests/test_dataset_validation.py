import json
from copy import deepcopy

import pytest

from app.comment_export.contract import ContractError
from app.comment_export.export import build_batch


def documents_for(batch):
    return {p.relative_to(batch).as_posix(): (
        '' if p.name == 'README.md' else
        [json.loads(line) for line in p.read_text(encoding='utf-8').split('\n')[:-1]]
        if p.suffix == '.jsonl' else json.loads(p.read_text(encoding='utf-8'))
    ) for p in batch.rglob('*') if p.is_file()}


@pytest.mark.parametrize('version', ['1.0.0', '2.0.0'])
def test_logical_documents_preserve_source_and_return_independent_manifest(frozen_case, tmp_path, version):
    from app.comment_export.dataset_validation import validate_documents
    rows, meta = frozen_case
    for value in [meta, *rows]:
        value['schema_version'] = version
    rows[0]['content']['text'] = 'nul\x00surrogate\ud800'
    docs = documents_for(build_batch(rows, meta, tmp_path / 'batch'))
    docs['manifest.json']['extension'] = {'number': 1.0}
    before = deepcopy(docs)
    result = validate_documents(docs)
    assert docs == before
    result['extension']['number'] = 2
    assert docs['manifest.json']['extension']['number'] == 1.0


@pytest.mark.parametrize('damage', ['missing', 'extra', 'copy', 'order', 'count', 'identity', 'readme', 'nan', 'non_json'])
def test_logical_documents_reject_invalid_source(frozen_case, tmp_path, damage):
    from app.comment_export.dataset_validation import validate_documents
    docs = documents_for(build_batch(*frozen_case, tmp_path / 'batch'))
    manifest = docs['manifest.json']
    path = docs[manifest['users'][0]['path']]['comments_path']
    if damage == 'missing':
        del docs[path]
    elif damage == 'extra':
        docs['extra.json'] = {}
    elif damage == 'copy':
        docs[path][0]['content']['text'] = 'changed'
    elif damage == 'order':
        docs[path].reverse()
    elif damage == 'count':
        manifest['counts']['comments'] += 1
    elif damage == 'identity':
        docs[path][0]['export_id'] = 'different'
    elif damage == 'readme':
        docs['README.md'] = 'nonempty'
    elif damage == 'nan':
        manifest['extension'] = float('nan')
    elif damage == 'non_json':
        manifest['extension'] = {1: 'not a JSON key'}
    with pytest.raises(ContractError):
        validate_documents(docs)


def test_source_numeric_encodings_are_not_rewritten(frozen_case, tmp_path):
    from app.comment_export.dataset_validation import validate_documents
    docs = documents_for(build_batch(*frozen_case, tmp_path / 'batch'))
    for path, values in docs.items():
        if path.endswith('.jsonl'):
            for row in values:
                row['extension'] = 1.0 if path.startswith('用户') else 1
    validate_documents(docs)
    for path, values in docs.items():
        if path.endswith('.jsonl'):
            assert type(values[0]['extension']) is (float if path.startswith('用户') else int)


def test_logical_documents_cannot_hide_context_gaps(frozen_case, tmp_path):
    from app.comment_export.dataset_validation import validate_documents
    docs = documents_for(build_batch(*frozen_case, tmp_path / 'batch'))
    for value in docs.values():
        if not isinstance(value, dict):
            continue
        if 'coverage' in value:
            value['coverage']['context_status'] = 'no_known_gaps'
            value['coverage']['reasons'] = []
        if 'context_status' in value:
            value['context_status'] = 'no_known_gaps'
            value['reasons'] = []
    with pytest.raises(ContractError):
        validate_documents(docs)


def test_file_wrapper_validates_each_record_once(frozen_case, tmp_path, monkeypatch):
    from app.comment_export import dataset_validation, validation
    batch = build_batch(*frozen_case, tmp_path / 'batch')
    calls = []
    original = dataset_validation.validate_record

    def track(kind, value):
        calls.append(kind)
        return original(kind, value)

    monkeypatch.setattr(dataset_validation, 'validate_record', track)
    monkeypatch.setattr(validation, 'validate_record', track)
    manifest = validation.validate_batch(batch)
    assert calls.count('manifest') == 1
    assert calls.count('comment') == 2 * manifest['counts']['comments']


@pytest.mark.parametrize('alias', ['a//b.json', 'a/./b.json', 'a/b.json/'])
def test_logical_documents_reject_path_aliases(frozen_case, tmp_path, alias):
    from app.comment_export.dataset_validation import validate_documents
    docs = documents_for(build_batch(*frozen_case, tmp_path / 'batch'))
    entry = docs['manifest.json']['threads'][0]
    docs[alias] = docs.pop(entry['path'])
    entry['path'] = alias
    with pytest.raises(ContractError, match='unsafe_path'):
        validate_documents(docs)


def test_logical_v2_rejects_nonportable_metadata_path(frozen_case, tmp_path):
    from app.comment_export.dataset_validation import validate_documents
    rows, meta = frozen_case
    for value in [meta, *rows]:
        value['schema_version'] = '2.0.0'
    docs = documents_for(build_batch(rows, meta, tmp_path / 'batch'))
    docs['BadDirectory/thread.json'] = {}
    with pytest.raises(ContractError, match='nonportable_export_path'):
        validate_documents(docs)


def test_file_metadata_parsed_by_role_not_extension(frozen_case, tmp_path):
    from app.comment_export.validation import validate_batch
    batch = build_batch(*frozen_case, tmp_path / 'batch')
    manifest_path = batch / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    entry = manifest['threads'][0]
    original = batch / entry['path']
    changed = original.with_suffix('.data')
    original.rename(changed)
    entry['path'] = changed.relative_to(batch).as_posix()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    assert validate_batch(batch) == manifest
