import json

import httpx
import pytest

from app.comment_export.signing import sign_main
from app.comment_export.source import (
    CollectionStopped,
    fetch_main,
    fetch_replies,
    get_signing_keys,
    resolve_video,
)


@pytest.mark.parametrize('episode,aid,bvid', [('123', '9001', 'BV1xx411c7mD'),
                                               ('456', '9002', 'BV1xx411c7mE')])
def test_episode_identity_is_resolved_and_checked(episode, aid, bvid):
    def handle(request):
        assert request.url.host == 'api.bilibili.com'
        assert request.extensions['timeout']['read'] == 20
        if request.url.path == '/pgc/view/web/season':
            assert request.url.params['ep_id'] == episode
            return httpx.Response(200, json={'code': 0, 'result': {'episodes': [
                {'id': int(episode), 'aid': int(aid), 'bvid': bvid}]}})
        assert request.url.params['aid'] == aid
        return httpx.Response(200, json={'code': 0, 'data': {
            'aid': int(aid), 'bvid': bvid, 'title': 'fictional'}})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        result = resolve_video(f'https://www.bilibili.com/bangumi/play/ep{episode}/?share=1',
                               client)
    assert result['source']['aid'] == aid
    assert result['source']['episode_id'] == episode
    assert result['canonical_url'] == f'https://www.bilibili.com/video/{bvid}'


def test_bv_aliases_and_large_ids():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        'code': 0, 'data': {'aid': 9007199254740993, 'bvid': 'BV1xx411c7mD'}}))) as client:
        a = resolve_video('https://www.bilibili.com/video/BV1xx411c7mD', client)
        b = resolve_video('https://www.bilibili.com/video/BV1xx411c7mD/?share=1', client)
    assert a == b
    assert a['source']['oid'] == '9007199254740993'


@pytest.mark.parametrize('url', [
    'http://www.bilibili.com/video/BV1xx411c7mD',
    'https://user@www.bilibili.com/video/BV1xx411c7mD',
    'https://www.bilibili.com:443/video/BV1xx411c7mD',
    'https://127.0.0.1/video/BV1xx411c7mD',
    'https://www.bilibili.com.evil.example/video/BV1xx411c7mD',
    'https://www.bilibili.com/video/av123',
])
def test_rejects_untrusted_url_without_request(url):
    def handle(_):
        pytest.fail('must reject before network')
    with (httpx.Client(transport=httpx.MockTransport(handle)) as client,
          pytest.raises(ValueError)):
        resolve_video(url, client)


def test_known_signing_vector_and_input_unchanged():
    params = {'foo': '114', 'bar': '514', 'baz': '1919810'}
    result = sign_main(params, '7cd084941338484aae1ad9425b84077c',
                       '4932caff0ff746eab6f01bf08b70ac45', 1702204169)
    assert result['w_rid'] == '6149fdadf571698ca7e6a567265cd0ee'
    assert result['wts'] == '1702204169'
    assert 'wts' not in params
    filtered = sign_main({'x': "a!b'c(d)e*f"}, 'a' * 32, 'b' * 32, 1)
    assert filtered['x'] == 'abcdef'


SOURCE = {'aid': '9001', 'oid': '9001', 'comment_type': 1}


def test_main_preserves_opaque_cursor_and_previews():
    cursor = '{"offset":"a+/=?"}'
    data = {'cursor': {'is_end': False, 'pagination_reply': {'next_offset': 'next'}},
            'replies': [{'rpid': 123, 'replies': [{'rpid': 124}]}]}
    def handle(request):
        assert request.url.path == '/x/v2/reply/wbi/main'
        assert json.loads(request.url.params['pagination_str']) == {'offset': cursor}
        assert request.url.params['mode'] == '2'
        return httpx.Response(200, json={'code': 0, 'data': data})
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        assert fetch_main(SOURCE, cursor, client, ('a' * 32, 'b' * 32)) == data


def test_replies_page_metadata():
    data = {'page': {'num': 1, 'size': 20, 'count': 0},
            'root': {'rpid': 123}, 'replies': None}
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={'code': 0, 'data': data}))) as client:
        assert fetch_replies(SOURCE, '123', 1, client) == data
        with pytest.raises(CollectionStopped):
            fetch_replies(SOURCE, '456', 1, client)
        with pytest.raises(CollectionStopped):
            fetch_replies(SOURCE, '123', 2, client)


@pytest.mark.parametrize('status,body', [(302, {}), (403, {}), (200, {'code': -352}),
    (200, {'code': 0, 'data': {}}),
    (200, {'code': 0, 'data': {'cursor': {'is_end': 'true'}, 'replies': []}})])
def test_main_rejects_denial_redirect_and_malformed_response(status, body):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            status, json=body, headers={'Location': 'https://evil.example'})),
            follow_redirects=True) as client, pytest.raises(CollectionStopped) as exc:
        fetch_main(SOURCE, None, client, ('a' * 32, 'b' * 32))
    assert str(exc.value) == exc.value.reason


def test_public_nav_keys_even_logged_out():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        'code': -101, 'data': {'wbi_img': {
            'img_url': 'https://i0.hdslb.com/bfs/wbi/' + 'a' * 32 + '.png',
            'sub_url': 'https://i0.hdslb.com/bfs/wbi/' + 'b' * 32 + '.png'}}}))) as client:
        assert get_signing_keys(client) == ('a' * 32, 'b' * 32)



def test_network_failure_is_safe():
    def handle(request):
        raise httpx.ConnectError('secret response detail', request=request)
    with (httpx.Client(transport=httpx.MockTransport(handle)) as client,
          pytest.raises(CollectionStopped, match='^network_error$')):
        fetch_main(SOURCE, None, client, ('a' * 32, 'b' * 32))


@pytest.mark.parametrize('data', [
    {'cursor': {'is_end': False}, 'replies': []},
    {'cursor': {'is_end': True}, 'replies': [1]},
    {'cursor': {'is_end': True}},
])
def test_incomplete_page_structure_is_never_end(data):
    with (httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={'code': 0, 'data': data}))) as client,
          pytest.raises(CollectionStopped, match='^invalid_response$')):
        fetch_main(SOURCE, None, client, ('a' * 32, 'b' * 32))


def test_resolved_identity_mismatch_stops():
    with (httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
            'code': 0, 'data': {'aid': 9001, 'bvid': 'BV1xx411c7mE'}}))) as client,
          pytest.raises(CollectionStopped, match='^video_identity_mismatch$')):
        resolve_video('https://www.bilibili.com/video/BV1xx411c7mD', client)
