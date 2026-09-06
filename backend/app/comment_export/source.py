"""Fixed Bilibili API adapters. No credential discovery or persistence."""

import inspect
import json
import re
import time
from contextlib import nullcontext
from dataclasses import replace
from functools import wraps
from typing import Any
from urllib.parse import urlsplit

import httpx

from .diagnostics import (
    ENDPOINT_PHASES,
    FailureDetail,
    classify_failure,
    parse_retry_after,
    utc_now,
)
from .signing import sign_main

_API = 'https://api.bilibili.com'
_UA = 'Mozilla/5.0 (compatible; BiliBiliTalksView/1.0)'
_ID = re.compile(r'[1-9][0-9]*\Z')
_BV = re.compile(r'BV[0-9A-Za-z]{10}\Z')


class CollectionStopped(RuntimeError):
    """Safe failure code only; never exposes headers or remote response text."""

    def __init__(self, reason: str, detail: FailureDetail | None = None):
        self.reason = reason
        self.detail = detail
        super().__init__(reason)


def _diagnose_adapter(function):
    signature = inspect.signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        client = signature.bind(*args, **kwargs).arguments['client']
        client.last_diagnostic = None
        try:
            return function(*args, **kwargs)
        except CollectionStopped as exc:
            if exc.detail is None and isinstance(client.last_diagnostic, FailureDetail):
                category = ('invalid_response' if exc.reason in
                            {'invalid_response', 'signing_keys_unavailable'} else 'local_validation')
                detail = replace(client.last_diagnostic, category=category, safe_reason=exc.reason)
                client.last_diagnostic = detail
                raise CollectionStopped(exc.reason, detail) from None
            raise
    return wrapped


def _id(value: Any) -> str:
    if type(value) not in (int, str) or not _ID.fullmatch(str(value)):
        raise CollectionStopped('invalid_response')
    return str(value)


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise CollectionStopped('invalid_response')
    return value


def _request_target(path: str, params: dict) -> tuple[str | None, dict]:
    video_id = None
    aid = params.get('oid', params.get('aid'))
    if aid is not None:
        video_id = 'bilibili:video:' + _id(aid)
    if path == '/x/v2/reply/reply':
        return video_id, {'root_id': _id(params['root']), 'page': int(params['pn'])}
    if path == '/x/v2/reply/wbi/main':
        return video_id, {'cursor': json.loads(params['pagination_str'])['offset']}
    if 'ep_id' in params:
        return video_id, {'episode_id': _id(params['ep_id'])}
    if 'bvid' in params:
        return video_id, {'bvid': params['bvid']}
    if 'aid' in params:
        return video_id, {'aid': _id(params['aid'])}
    return video_id, {}


def _request(client: httpx.Client, path: str, params: dict | None = None,
             *, public_nav: bool = False, phase: str | None = None,
             target: dict | None = None) -> dict:
    if path not in ENDPOINT_PHASES:
        raise ValueError('unsupported_api_endpoint')
    video_id, derived_target = _request_target(path, params or {})
    target = derived_target if target is None else target
    detail = FailureDetail(phase=phase or ENDPOINT_PHASES[path], endpoint=path,
                           video_id=video_id, target=target)
    client.last_diagnostic = detail
    policy = getattr(client, 'access_policy', None)
    with policy.attempt(path, target) if policy is not None else nullcontext():
        try:
            response = client.get(_API + path, params=params,
                                  headers={'User-Agent': _UA}, timeout=20,
                                  follow_redirects=False)
        except httpx.HTTPError:
            detail = replace(detail, observed_at=utc_now(), category='network_error',
                             safe_reason='network_error')
            client.last_diagnostic = detail
            raise CollectionStopped('network_error', detail) from None
        try:
            body = response.json()
        except (ValueError, UnicodeError):
            body = None
        code = body.get('code') if isinstance(body, dict) else None
        code = code if type(code) is int else None
        detail = replace(detail, observed_at=utc_now())
        detail = replace(detail, http_status=response.status_code, api_code=code,
            retry_after_at=parse_retry_after(response.headers.get('Retry-After'), detail.observed_at),
            category=classify_failure(response.status_code, code, path),
            safe_reason='response_received')
        client.last_diagnostic = detail
        if response.status_code != 200:
            reason = ('access_restricted' if response.status_code in
                      (301, 302, 303, 307, 308, 401, 403, 412, 429) else 'http_error')
        elif not isinstance(body, dict) or code is None:
            reason = 'invalid_response'
        elif code != 0 and not (public_nav and code == -101):
            reason = 'source_reply_unavailable' if detail.category == 'resource_unavailable' else 'access_restricted'
        else:
            return body
        detail = replace(detail, safe_reason=reason)
        client.last_diagnostic = detail
        raise CollectionStopped(reason, detail)


def login_check(client: httpx.Client) -> str:
    try:
        body = _request(client, '/x/web-interface/nav', public_nav=True, phase='login_check')
    except CollectionStopped:
        return 'unknown'
    data = body.get('data')
    value = data.get('isLogin') if isinstance(data, dict) else None
    if type(value) is not bool:
        client.last_diagnostic = replace(client.last_diagnostic, category='invalid_response',
                                         safe_reason='invalid_response')
        return 'unknown'
    return 'logged_in' if value else 'logged_out'


@_diagnose_adapter
def resolve_video(url: str, client: httpx.Client) -> dict:
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc != 'www.bilibili.com'
            or parsed.fragment or any(c.isspace() for c in url)):
        raise ValueError('unsupported_video_url')
    bv_match = re.fullmatch(r'/video/(BV[0-9A-Za-z]{10})/?', parsed.path)
    ep_match = re.fullmatch(r'/bangumi/play/ep([1-9][0-9]*)/?', parsed.path)
    if not (bv_match or ep_match):
        raise ValueError('unsupported_video_url')
    episode_id = ep_match[1] if ep_match else None
    expected_aid = None
    expected_bv = bv_match[1] if bv_match else None
    if episode_id:
        season = _object(_request(client, '/pgc/view/web/season',
                                  {'ep_id': episode_id}).get('result'))
        episodes = season.get('episodes')
        if not isinstance(episodes, list):
            raise CollectionStopped('invalid_response')
        matches = [ep for ep in episodes if isinstance(ep, dict)
                   and str(ep.get('id')) == episode_id]
        if len(matches) != 1:
            raise CollectionStopped('video_identity_mismatch')
        expected_aid = _id(matches[0].get('aid'))
        expected_bv = matches[0].get('bvid')
    params = {'aid': expected_aid} if expected_aid else {'bvid': expected_bv}
    video = _object(_request(client, '/x/web-interface/view', params).get('data'))
    aid = _id(video.get('aid'))
    bvid = video.get('bvid')
    if not isinstance(bvid, str) or not _BV.fullmatch(bvid):
        raise CollectionStopped('invalid_response')
    if (expected_aid and expected_aid != aid) or (expected_bv and expected_bv != bvid):
        raise CollectionStopped('video_identity_mismatch')
    title = video.get('title')
    if title is not None and not isinstance(title, str):
        raise CollectionStopped('invalid_response')
    return {'source': {'platform': 'bilibili', 'aid': aid, 'oid': aid, 'bvid': bvid,
                       'episode_id': episode_id, 'comment_type': 1},
            'canonical_url': f'https://www.bilibili.com/video/{bvid}', 'title': title}


@_diagnose_adapter
def get_signing_keys(client: httpx.Client) -> tuple[str, str]:
    data = _object(_request(client, '/x/web-interface/nav', public_nav=True).get('data'))
    images = _object(data.get('wbi_img'))
    keys = []
    for name in ('img_url', 'sub_url'):
        url = images.get(name)
        if not isinstance(url, str):
            raise CollectionStopped('signing_keys_unavailable')
        key = urlsplit(url).path.rsplit('/', 1)[-1].split('.')[0]
        if not re.fullmatch('[0-9a-fA-F]{32}', key):
            raise CollectionStopped('signing_keys_unavailable')
        keys.append(key)
    return keys[0], keys[1]


def _source_params(source: dict) -> dict[str, str]:
    oid = _id(source.get('oid'))
    if oid != _id(source.get('aid')) or source.get('comment_type') != 1:
        raise ValueError('unsupported_comment_source')
    return {'oid': oid, 'type': '1'}


def _replies(data: dict) -> None:
    if 'replies' not in data or (data['replies'] is not None and
                               not isinstance(data['replies'], list)):
        raise CollectionStopped('invalid_response')
    if any(not isinstance(reply, dict) for reply in (data['replies'] or [])):
        raise CollectionStopped('invalid_response')


@_diagnose_adapter
def fetch_main(source: dict, cursor: str | None, client: httpx.Client,
               signing_keys: tuple[str, str]) -> dict:
    params = {**_source_params(source), 'mode': '2',
              'pagination_str': json.dumps({'offset': cursor or ''}, separators=(',', ':'))}
    params = sign_main(params, *signing_keys, timestamp=int(time.time()))
    data = _object(_request(client, '/x/v2/reply/wbi/main', params,
                            target={'cursor': cursor or ''}).get('data'))
    _replies(data)
    position = _object(data.get('cursor'))
    if type(position.get('is_end')) is not bool:
        raise CollectionStopped('invalid_response')
    if not position['is_end']:
        pagination = _object(position.get('pagination_reply'))
        if not isinstance(pagination.get('next_offset'), str) or not pagination['next_offset']:
            raise CollectionStopped('invalid_response')
    return data


@_diagnose_adapter
def fetch_replies(source: dict, root_id: str, page: int, client: httpx.Client) -> dict:
    root_id = _id(root_id)
    if type(page) is not int or page < 1:
        raise ValueError('invalid_reply_page')
    params = {**_source_params(source), 'root': root_id, 'pn': str(page), 'ps': '20'}
    data = _object(_request(client, '/x/v2/reply/reply', params).get('data'))
    _replies(data)
    metadata = _object(data.get('page'))
    if (type(metadata.get('num')) is not int or metadata['num'] != page
            or type(metadata.get('size')) is not int or metadata['size'] != 20
            or type(metadata.get('count')) is not int or metadata['count'] < 0):
        raise CollectionStopped('invalid_response')
    root = _object(data.get('root'))
    if _id(root.get('rpid_str', root.get('rpid'))) != root_id:
        raise CollectionStopped('video_identity_mismatch')
    for reply in data['replies'] or []:
        observed = reply.get('root_str', reply.get('root'))
        if observed is not None and _id(observed) != root_id:
            raise CollectionStopped('video_identity_mismatch')
    return data
