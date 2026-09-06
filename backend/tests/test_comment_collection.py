import httpx

from app.comment_export import collector


def raw(cid, root=0, parent=0):
    return {"rpid": cid, "root": root, "parent": parent, "member": {"mid": 1, "uname": "用户"},
            "content": {"message": "评论"}, "ctime": 1, "like": 0, "rcount": 0}


def setup_source(monkeypatch):
    monkeypatch.setattr(collector, "resolve_video", lambda url, client: {
        "source": {"platform": "bilibili", "aid": "1", "oid": "1", "bvid": "BVfake",
                   "episode_id": None, "comment_type": 1},
        "canonical_url": "https://www.bilibili.com/video/BVfake", "title": "样例"})
    monkeypatch.setattr(collector, "get_signing_keys", lambda client: ("a", "b"))
    monkeypatch.setattr(collector, "fetch_main", lambda *args: {
        "replies": [raw(100)], "cursor": {"is_end": True, "all_count": 1}})


def test_zero_reply_floor_is_actually_checked(monkeypatch, tmp_path):
    setup_source(monkeypatch)
    called = []
    def replies(source, root, page, client):
        called.append((root, page))
        return {"root": raw(100), "page": {"num": page, "size": 20, "count": 0}, "replies": []}
    monkeypatch.setattr(collector, "fetch_replies", replies)
    with httpx.Client() as client:
        rows, meta = collector.collect("https://www.bilibili.com/video/BVfake", tmp_path,
                                       client, 100, False)
    assert called == [("100", 1)]
    assert len(rows) == 1
    assert meta["coverage"]["status"] == "verified"


def test_denial_keeps_partial_checkpoint(monkeypatch, tmp_path):
    setup_source(monkeypatch)
    def denied(*args):
        raise collector.CollectionStopped("access_restricted")
    monkeypatch.setattr(collector, "fetch_replies", denied)
    with httpx.Client() as client:
        rows, meta = collector.collect("https://www.bilibili.com/video/BVfake", tmp_path,
                                       client, 100, False)
    assert len(rows) == 1
    assert meta["coverage"]["status"] == "partial"
    assert "access_restricted" in meta["coverage"]["reasons"]


def test_main_count_change_requires_independent_reread(monkeypatch, tmp_path):
    setup_source(monkeypatch)
    called = []
    def replies(source, root, page, client):
        called.append(page)
        return {'root': raw(100), 'page': {'num': page, 'size': 20, 'count': 1},
                'replies': [raw(101, 100, 100)]}
    monkeypatch.setattr(collector, 'fetch_replies', replies)
    with httpx.Client() as client:
        _, meta = collector.collect('url', tmp_path, client, 100)
    assert called == [1, 1]
    assert meta['coverage']['status'] == 'verified'


def test_root_context_change_during_reread_stays_partial(monkeypatch, tmp_path):
    setup_source(monkeypatch)
    called = []
    def replies(source, root, page, client):
        called.append(page)
        root_row = raw(100)
        root_row['content']['message'] = str(len(called))
        return {'root': root_row, 'page': {'num': page, 'size': 20, 'count': 1},
                'replies': [raw(101, 100, 100)]}
    monkeypatch.setattr(collector, 'fetch_replies', replies)
    with httpx.Client() as client:
        _, meta = collector.collect('url', tmp_path, client, 100)
    assert len(called) == 2
    assert meta['coverage']['status'] == 'partial'
    assert 'count_changed' in meta['coverage']['reasons']


def test_failed_page_does_not_advance_durable_progress(monkeypatch, tmp_path):
    setup_source(monkeypatch)
    original = collector.Checkpoint.commit_page
    def fail_page(self, key, comments, progress):
        if key.startswith('main:'):
            raise collector.ContractError('identity_conflict')
        return original(self, key, comments, progress)
    monkeypatch.setattr(collector.Checkpoint, 'commit_page', fail_page)
    with httpx.Client() as client:
        rows, meta = collector.collect('url', tmp_path, client, 100)
    cp = collector.Checkpoint(tmp_path / 'work.sqlite3')
    assert not cp.get_progress().get('main_done')
    assert not cp.get_progress().get('visited')
    assert cp.get_progress()['blocked']
    assert not rows
    assert meta['coverage']['status'] == 'partial'
    cp.close()


def test_crash_after_committed_main_recovers_metadata_and_cursor(monkeypatch, tmp_path):
    import pytest

    setup_source(monkeypatch)
    original = collector.Checkpoint.commit_page
    crashed = []
    def crash_after_commit(self, key, comments, progress):
        original(self, key, comments, progress)
        if key.startswith('main:') and not crashed:
            crashed.append(True)
            raise KeyboardInterrupt
    monkeypatch.setattr(collector.Checkpoint, 'commit_page', crash_after_commit)
    with httpx.Client() as client, pytest.raises(KeyboardInterrupt):
        collector.collect('url', tmp_path, client, 100)
    cp = collector.Checkpoint(tmp_path / 'work.sqlite3')
    assert cp.get_progress()['main_done']
    assert cp.freeze()[1]['coverage']['main_pagination'] == 'verified'
    assert '100' in cp.freeze()[1]['_threads']
    cp.close()
    monkeypatch.setattr(collector, 'fetch_main', lambda *args: pytest.fail('committed page repeated'))
    monkeypatch.setattr(collector, 'fetch_replies', lambda source, root, page, client: {
        'root': raw(100), 'page': {'num': page, 'size': 20, 'count': 0}, 'replies': []})
    with httpx.Client() as client:
        rows, meta = collector.collect('url', tmp_path, client, 100, resume=True)
    assert len(rows) == 1
    assert meta['coverage']['status'] == 'verified'


def test_request_debit_survives_failed_page(monkeypatch, tmp_path):
    setup_source(monkeypatch)
    def fail_main(source, cursor, client, keys):
        client.get('https://api.bilibili.com/x/web-interface/nav')
        raise collector.ContractError('identity_conflict')
    monkeypatch.setattr(collector, 'fetch_main', fail_main)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200))) as client:
        collector.collect('url', tmp_path, client, 100)
    cp = collector.Checkpoint(tmp_path / 'work.sqlite3')
    assert cp.get_progress()['requests'] == 1
    assert not cp.get_progress().get('main_done')
    assert cp.get_progress()['blocked']
    cp.close()
