from datetime import UTC, datetime

import httpx
import pytest

from app.comment_export.diagnostics import FailureDetail, classify_failure, parse_retry_after
from app.comment_export.source import CollectionStopped, _request, login_check


@pytest.mark.parametrize('status,code,category', [
    (429, None, 'rate_limited'), (401, None, 'authentication_required'),
    (403, None, 'access_restricted'), (412, None, 'access_restricted'),
    (503, None, 'source_unavailable'), (302, None, 'request_rejected'),
    (404, None, 'request_rejected'), (200, -352, 'access_restricted'),
])
def test_classification(status, code, category):
    assert classify_failure(status, code, '/x/v2/reply/reply') == category


def test_retry_after_uses_injected_time_and_rejects_invalid_values():
    observed = datetime(2026, 9, 6, 1, tzinfo=UTC)
    assert parse_retry_after('60', observed) == '2026-09-06T01:01:00Z'
    assert parse_retry_after('Sun, 06 Sep 2026 02:00:00 GMT', observed) == '2026-09-06T02:00:00Z'
    assert parse_retry_after('0', observed) == '2026-09-06T01:00:00Z'
    for value in ('-1', '1.5', 'bad', None):
        assert parse_retry_after(value, observed) is None


def test_public_detail_redacts_cursor_and_disallows_unknown_fields():
    detail = FailureDetail(phase='main', endpoint='/x/v2/reply/wbi/main',
                           target={'cursor': 'secret-cursor'}, category='rate_limited')
    assert detail.to_dict()['target']['cursor'] == 'secret-cursor'
    assert 'secret-cursor' not in str(detail.to_public_dict())
    assert len(detail.to_public_dict()['target']['cursor_digest']) == 64
    with pytest.raises(TypeError):
        FailureDetail(cookie='secret')


@pytest.mark.parametrize('status,code,category', [
    (429, -999, 'rate_limited'), (401, None, 'authentication_required'),
    (403, -352, 'access_restricted'), (503, None, 'source_unavailable'),
    (302, None, 'request_rejected'), (200, -352, 'access_restricted')])
def test_request_preserves_actual_codes_without_remote_secrets(status, code, category):
    body = {'message': 'secret-response-body', 'code': code}
    with (httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            status, json=body, headers={'Set-Cookie': 'secret-cookie', 'Retry-After': '60'}))) as client,
          pytest.raises(CollectionStopped) as caught):
        _request(client, '/x/v2/reply/reply', {'oid': '123', 'root': '456', 'pn': '2'})
    detail = caught.value.detail
    assert detail.http_status == status
    assert detail.api_code == code
    assert detail.category == category
    assert detail.phase == 'replies'
    assert detail.target == {'root_id': '456', 'page': 2}
    assert detail.video_id == 'bilibili:video:123'
    assert detail.retry_after_at is not None
    assert 'secret' not in str(detail.to_public_dict()) + str(caught.value)


@pytest.mark.parametrize('body,expected', [
    ({'code': 0, 'data': {'isLogin': True, 'uname': 'secret'}}, 'logged_in'),
    ({'code': -101, 'data': {'isLogin': False}}, 'logged_out'),
    ({'code': 0, 'data': {'isLogin': 0}}, 'unknown'),
    ({'code': 0, 'data': {}}, 'unknown'),
])
def test_login_check_requires_explicit_boolean(body, expected):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        assert login_check(client) == expected


def test_login_http_failure_is_unknown_not_logged_out():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(401))) as client:
        assert login_check(client) == 'unknown'
        assert client.last_diagnostic.http_status == 401


def test_access_policy_observes_business_failure_inside_attempt():
    from contextlib import contextmanager

    failures = []
    class Policy:
        @contextmanager
        def attempt(self, endpoint, target):
            try:
                yield
            except CollectionStopped as exc:
                failures.append(exc.detail)
                raise
    with (httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={'code': -352, 'message': 'secret'}))) as client,
          pytest.raises(CollectionStopped)):
        client.access_policy = Policy()
        _request(client, '/x/v2/reply/reply', {'oid': '123', 'root': '456', 'pn': '2'})
    assert failures[0].api_code == -352


def test_structural_error_keeps_actual_response_context():
    from app.comment_export.source import fetch_replies

    with (httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={'code': 0, 'data': {'replies': [], 'page': {}}}))) as client,
          pytest.raises(CollectionStopped) as caught):
        fetch_replies({'aid': '1', 'oid': '1', 'comment_type': 1}, '2', 1, client)
    assert caught.value.detail.http_status == 200
    assert caught.value.detail.api_code == 0
    assert caught.value.detail.category == 'invalid_response'
    assert caught.value.detail.target == {'root_id': '2', 'page': 1}


def test_network_exception_text_is_not_retained():
    def fail(request):
        raise httpx.ConnectError('secret-network-info', request=request)
    with (httpx.Client(transport=httpx.MockTransport(fail)) as client,
          pytest.raises(CollectionStopped) as caught):
        _request(client, '/x/web-interface/nav')
    assert caught.value.detail.http_status is None
    assert caught.value.detail.api_code is None
    assert 'secret' not in str(caught.value.detail.to_public_dict()) + str(caught.value)


@pytest.mark.parametrize('fields', [
    {'category': 'secret-cookie'}, {'phase': 'secret-phase'},
    {'failure_id': 'secret-id'}, {'failure_id': True},
    {'observed_at': '2026-02-30T00:00:00Z'}, {'observed_at': '2026-09-06T00:00:00+00:00'},
    {'observed_at': 1}, {'retry_after_at': 'secret-header'}, {'retry_after_at': False},
    {'http_status': True}, {'http_status': 99}, {'http_status': 600}, {'http_status': '429'},
    {'api_code': False}, {'api_code': '-352'},
    {'checkpoint_revision': -1}, {'checkpoint_revision': True}, {'checkpoint_revision': '1'},
    {'video_id': 'bilibili:video:01'}, {'video_id': 'secret-video'}, {'video_id': 1},
    {'target': []}, {'target': {'cursor': 'safe', 'cookie': 'secret'}},
    {'safe_reason': None}, {'category': []}, {'phase': None}, {'endpoint': []},
])
def test_damaged_diagnostic_fields_are_rejected(fields):
    with pytest.raises(ValueError, match='^invalid_diagnostic_'):
        FailureDetail(**fields)


def test_valid_detail_types_and_utc_timestamps_round_trip():
    detail = FailureDetail(http_status=429, api_code=-352, checkpoint_revision=0,
        observed_at='2026-09-06T00:00:00Z', retry_after_at='2026-09-06T00:30:00Z',
        category='rate_limited', phase='main', video_id='bilibili:video:9007199254740993',
        safe_reason='response_received')
    assert FailureDetail(**detail.to_dict()) == detail
