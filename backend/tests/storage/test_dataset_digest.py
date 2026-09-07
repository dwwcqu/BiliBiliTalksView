import pytest

from app.comment_export.export import build_batch
from app.comment_export.layout import user_directory
from app.storage.frozen import canonical_digest, freeze_batch


@pytest.mark.parametrize(('version', 'expected'), [
    ('1.0.0', '1a476c857ccbf93b877da956fbea4eb0b74bd0e6805289691f5eadbc49c129ae'),
    ('2.0.0', '6ebce9383e3f6770f4fce1b7d0a92c7416ca90a9374ec63ee4422aaceadefc43'),
])
def test_builder_digest_matches_pre_refactor_golden(frozen_case, tmp_path, version, expected):
    from app.comment_export.dataset_builder import build_dataset

    rows, metadata = frozen_case
    for value in [metadata, *rows]:
        value['schema_version'] = version
    dataset = build_dataset(rows, metadata)
    batch = build_batch(rows, metadata, tmp_path / 'batch')
    assert dataset.digest == expected == canonical_digest(batch)
    with freeze_batch(batch, tmp_path / 'work') as frozen:
        assert frozen.digest == expected
        assert dict(frozen.dataset.iter_documents()) == dict(dataset.iter_documents())
        path = dataset.read_document(dataset.manifest['threads'][0]['path'])['comments_path']
        assert frozen.read_lines(path) == dataset.read_lines(path)


def test_digest_ignores_key_order_but_protects_extension_encoding(frozen_case):
    from app.comment_export.dataset import ValidatedDataset
    from app.comment_export.dataset_builder import build_dataset

    documents = dict(build_dataset(*frozen_case).iter_documents())
    documents['manifest.json']['extension'] = {'b': 1.0, 'a': '\u4e2d\ud800\x00'}
    first = ValidatedDataset.from_documents(documents)
    documents['manifest.json']['extension'] = {'a': '\u4e2d\ud800\x00', 'b': 1.0}
    assert ValidatedDataset.from_documents(documents).digest == first.digest
    documents['manifest.json']['extension']['b'] = 1
    assert ValidatedDataset.from_documents(documents).digest != first.digest


@pytest.mark.parametrize('role', ['metadata', 'lines'])
def test_file_adapter_preserves_unusual_source_suffixes(frozen_case, tmp_path, role):
    import json

    from app.comment_export.dataset import ValidatedDataset
    from app.comment_export.export import write_dataset, write_json

    source = build_batch(*frozen_case, tmp_path / 'source')
    manifest_path = source / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    entry = manifest['threads'][1]
    info_path = source / entry['path']
    info = json.loads(info_path.read_text(encoding='utf-8'))
    if role == 'metadata':
        new = entry['path'].replace('thread.json', 'thread.data')
        info_path.rename(source / new)
        entry['path'] = new
        write_json(manifest_path, manifest)
    else:
        new = info['comments_path'].replace('comments.jsonl', 'comments.data')
        (source / info['comments_path']).rename(source / new)
        info['comments_path'] = new
        write_json(info_path, info)
    expected = canonical_digest(source)
    with freeze_batch(source, tmp_path / 'work') as frozen:
        assert frozen.digest == expected
        logical = ValidatedDataset.from_documents(dict(frozen.iter_documents()))
        assert logical.digest == expected
        if role == 'metadata':
            assert frozen.read_document(new) == info
        else:
            assert frozen.read_lines(new)[0]['comment_id'] == '200'
        output = write_dataset(logical, tmp_path / 'rewritten')
        assert canonical_digest(output) == expected


def test_file_adapter_keeps_different_numeric_projection_representations(frozen_case, tmp_path):
    import json

    from app.comment_export.export import write_json

    source = build_batch(*frozen_case, tmp_path / 'source')
    for path in source.rglob('*.jsonl'):
        rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
        for row in rows:
            row['extension'] = 1.0 if path.relative_to(source).parts[0] == user_directory('1.0.0') else 1
        write_json(path, rows, lines=True)
    expected = canonical_digest(source)
    with freeze_batch(source, tmp_path / 'work') as frozen:
        assert frozen.digest == expected
        for path, values in frozen.iter_documents():
            if path.endswith('.jsonl'):
                assert type(values[0]['extension']) is (
                    float if path.startswith(user_directory('1.0.0')) else int
                )


def test_digest_preserves_unclassified_row_order_and_ignores_evidence(frozen_case, tmp_path):
    from app.comment_export.dataset import ValidatedDataset
    from app.comment_export.dataset_builder import build_dataset

    rows, metadata = frozen_case
    metadata['_unclassified'] = [
        {'reason': 'invalid_comment_id', 'observed_at': '2026-09-05T09:00:00Z',
         'source_comment_id': value, 'source_root_id': None,
         'author': {'uid': None, 'nickname': None},
         'content': {'text': None, 'images': [], 'emotes': []}}
        for value in ['bad-first', 'bad-second']
    ]
    dataset = build_dataset(rows, metadata)
    batch = build_batch(rows, metadata, tmp_path / 'batch')
    assert dataset.digest == canonical_digest(batch)
    documents = dict(dataset.iter_documents())
    assert ValidatedDataset.from_documents(documents, evidence={'different': 1}).digest == dataset.digest
    documents['unclassified.jsonl'].reverse()
    assert ValidatedDataset.from_documents(documents).digest != dataset.digest


@pytest.mark.parametrize('count', [0, 2])
def test_logical_non_jsonl_lines_reject_legacy_unparseable_content(frozen_case, count):
    from app.comment_export.contract import ContractError
    from app.comment_export.dataset import ValidatedDataset
    from app.comment_export.dataset_builder import build_dataset

    rows, metadata = frozen_case
    metadata['_unclassified'] = [
        {'reason': 'invalid_comment_id', 'observed_at': '2026-09-05T09:00:00Z',
         'source_comment_id': str(i), 'source_root_id': None,
         'author': {'uid': None, 'nickname': None},
         'content': {'text': None, 'images': [], 'emotes': []}}
        for i in range(count)
    ]
    docs = dict(build_dataset(rows, metadata).iter_documents())
    docs['manifest.json']['unclassified_path'] = 'unclassified.data'
    docs['unclassified.data'] = docs.pop('unclassified.jsonl', [])
    with pytest.raises(ContractError, match='invalid_json'):
        ValidatedDataset.from_documents(docs)


def test_logical_metadata_jsonl_roundtrip_keeps_both_roles(frozen_case, tmp_path):
    from app.comment_export.dataset import ValidatedDataset
    from app.comment_export.dataset_builder import build_dataset
    from app.comment_export.export import write_dataset

    docs = dict(build_dataset(*frozen_case).iter_documents())
    entry = docs['manifest.json']['threads'][1]
    info = docs.pop(entry['path'])
    info.update(docs[info['comments_path']][0])
    entry['path'] = entry['path'].replace('thread.json', 'thread.jsonl')
    docs[entry['path']] = info
    logical = ValidatedDataset.from_documents(docs)
    batch = write_dataset(logical, tmp_path / 'batch')
    with freeze_batch(batch, tmp_path / 'work') as frozen:
        assert frozen.digest == logical.digest == canonical_digest(batch)
        assert frozen.read_document(entry['path']) == info


def test_logical_alternate_unclassified_jsonl_retains_legacy_comment_check(frozen_case):
    from app.comment_export.contract import ContractError
    from app.comment_export.dataset import ValidatedDataset
    from app.comment_export.dataset_builder import build_dataset

    rows, metadata = frozen_case
    metadata['_unclassified'] = [
        {'reason': 'invalid_comment_id', 'observed_at': '2026-09-05T09:00:00Z',
         'source_comment_id': None, 'source_root_id': None,
         'author': {'uid': None, 'nickname': None},
         'content': {'text': None, 'images': [], 'emotes': []}}
    ]
    docs = dict(build_dataset(rows, metadata).iter_documents())
    docs['manifest.json']['unclassified_path'] = 'alternate.jsonl'
    docs['alternate.jsonl'] = docs.pop('unclassified.jsonl')
    with pytest.raises(ContractError):
        ValidatedDataset.from_documents(docs)



def test_file_named_default_directory_roundtrips(frozen_case, tmp_path):
    from app.comment_export.dataset_builder import build_dataset
    from app.comment_export.export import write_dataset, write_json
    from app.comment_export.layout import thread_directory

    rows, metadata = frozen_case
    directory = thread_directory(metadata['schema_version'])
    documents = dict(build_dataset(rows, metadata).iter_documents())
    for index, entry in enumerate(documents['manifest.json']['threads']):
        info = documents.pop(entry['path'])
        comments = documents.pop(info['comments_path'])
        entry['path'] = directory if index == 0 else f'other/{index}.json'
        info['comments_path'] = f'other/{index}.jsonl'
        documents[entry['path']] = info
        documents[info['comments_path']] = comments
    source = tmp_path / 'source'
    source.mkdir()
    for path, value in documents.items():
        if path == 'README.md':
            (source / path).write_bytes(b'')
        else:
            write_json(source / path, value, lines=isinstance(value, list))
    with freeze_batch(source, tmp_path / 'work') as frozen:
        output = write_dataset(frozen.dataset, tmp_path / 'output')
        assert (output / directory).is_file()
        assert canonical_digest(output) == frozen.digest
