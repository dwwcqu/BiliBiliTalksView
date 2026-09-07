from copy import deepcopy

import pytest

from app.comment_export.contract import ContractError


def test_dataset_builder_freezes_inputs_and_all_public_views(frozen_case, tmp_path):
    from app.comment_export.dataset_builder import build_dataset
    from app.comment_export.export import write_dataset

    rows, metadata = frozen_case
    metadata['_refresh'] = {'nested': {'roots': ['100']}}
    dataset = build_dataset(rows, metadata)
    original = dict(dataset.iter_documents())
    evidence = deepcopy(metadata)
    digest = dataset.digest
    rows[0]['content']['text'] = 'changed input'
    metadata['_refresh']['nested']['roots'].clear()
    dataset.manifest['source']['aid'] = '9'
    dataset.evidence['_refresh']['nested']['roots'].clear()
    for _, document in dataset.iter_documents():
        if isinstance(document, dict):
            document.clear()
        elif isinstance(document, list):
            document[0]['content']['text'] = 'changed view'
    next(dataset.iter_comments('100'))['author']['uid'] = '9'
    assert dict(dataset.iter_documents()) == original
    assert dataset.evidence == evidence
    assert dataset.digest == digest
    with pytest.raises(AttributeError):
        dataset.digest = 'replacement'
    destination = write_dataset(dataset, tmp_path / 'batch')
    from app.comment_export.validation import validate_batch
    assert validate_batch(destination) == original['manifest.json']


def test_from_documents_preserves_numeric_projection_encodings(frozen_case):
    from app.comment_export.dataset import ValidatedDataset
    from app.comment_export.dataset_builder import build_dataset

    docs = dict(build_dataset(*frozen_case).iter_documents())
    for path, value in docs.items():
        if path.endswith('.jsonl'):
            for row in value:
                row['extension'] = {'value': 1.0 if path.startswith('用户') else 1}
    dataset = ValidatedDataset.from_documents(docs, evidence={'nested': [1]})
    before = dataset.digest
    for path, value in dataset.iter_documents():
        if path.endswith('.jsonl'):
            assert type(value[0]['extension']['value']) is (
                float if path.startswith('用户') else int
            )
    docs['manifest.json']['source']['aid'] = '9'
    assert dataset.manifest['source']['aid'] == '10001'
    assert dataset.digest == before


def test_dataset_rejects_invalid_document_set(frozen_case):
    from app.comment_export.dataset import ValidatedDataset
    from app.comment_export.dataset_builder import build_dataset

    documents = dict(build_dataset(*frozen_case).iter_documents())
    documents['extra.json'] = {}
    with pytest.raises(ContractError, match='unindexed_files'):
        ValidatedDataset.from_documents(documents)


@pytest.mark.parametrize('invalid', [{1}, bytearray(b'x'), {'nested': float('nan')}])
def test_dataset_rejects_mutable_non_json_evidence(frozen_case, invalid):
    from app.comment_export.dataset import ValidatedDataset
    from app.comment_export.dataset_builder import build_dataset

    documents = dict(build_dataset(*frozen_case).iter_documents())
    with pytest.raises(ContractError, match='invalid_json'):
        ValidatedDataset.from_documents(documents, evidence={'proof': invalid})


def test_identical_projection_records_share_private_body(frozen_case):
    from app.comment_export.dataset_builder import build_dataset

    dataset = build_dataset(*frozen_case)
    docs = dataset._documents
    manifest = docs['manifest.json']
    thread = docs[manifest['threads'][0]['path']]
    user = docs[manifest['users'][0]['path']]
    assert docs[thread['comments_path']][0] is docs[user['comments_path']][0]



def test_logical_file_cannot_also_be_parent_directory(frozen_case):
    from app.comment_export.dataset import ValidatedDataset
    from app.comment_export.dataset_builder import build_dataset
    from app.comment_export.layout import thread_directory

    rows, metadata = frozen_case
    directory = thread_directory(metadata['schema_version'])
    documents = dict(build_dataset(rows, metadata).iter_documents())
    entry = documents['manifest.json']['threads'][0]
    documents[directory] = documents.pop(entry['path'])
    entry['path'] = directory
    with pytest.raises(ContractError, match='file_directory_conflict'):
        ValidatedDataset.from_documents(documents)


@pytest.mark.parametrize('version', ['1.0.0', '2.0.0'])
def test_empty_dataset_keeps_compatibility_directories(frozen_case, tmp_path, version):
    from app.comment_export.dataset_builder import build_dataset
    from app.comment_export.export import write_dataset
    from app.comment_export.layout import thread_directory, user_directory
    from app.comment_export.validation import validate_batch

    _, metadata = frozen_case
    metadata['schema_version'] = version
    dataset = build_dataset([], metadata)
    output = write_dataset(dataset, tmp_path / 'output')
    assert (output / thread_directory(version)).is_dir()
    assert (output / user_directory(version)).is_dir()
    assert validate_batch(output) == dataset.manifest
