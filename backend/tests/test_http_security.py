import asyncio
from types import SimpleNamespace

from starlette.requests import Request

from app.api.security import BodyLimitMiddleware, client_key, trusted_networks


def key(peer, forwarded="", trusted=""):
    state = SimpleNamespace(
        rate_limit_secret="stable-key", trusted_proxies=trusted_networks(trusted)
    )
    request = Request(
        {
            "type": "http",
            "client": (peer, 1000),
            "app": SimpleNamespace(state=state),
            "headers": [
                (b"x-forwarded-for", item.encode())
                for item in ([forwarded] if isinstance(forwarded, str) else forwarded)
            ],
        }
    )
    return client_key(request)


def test_untrusted_forwarding_headers_do_not_change_identity():
    assert key("192.0.2.1", "198.51.100.1") == key("192.0.2.1")
    assert len(key("192.0.2.1")) == 64
    assert "192.0.2.1" not in key("192.0.2.1")


def test_only_right_hand_trusted_proxy_chain_is_accepted():
    assert key("10.0.0.2", "203.0.113.99, 198.51.100.7, 10.0.0.1", "10.0.0.0/8") == key(
        "198.51.100.7"
    )
    assert key("10.0.0.2", "198.51.100.7, invalid", "10.0.0.0/8") == key("10.0.0.2")


def test_body_limit_stops_receiving_as_soon_as_threshold_is_crossed():
    messages, calls = [], []

    async def app(*_):
        raise AssertionError("oversized request reached application")

    async def receive():
        calls.append(1)
        assert len(calls) <= 2
        return {
            "type": "http.request",
            "body": b"x" * (4096 if len(calls) == 1 else 4097),
            "more_body": True,
        }

    async def send(message):
        messages.append(message)

    asyncio.run(
        BodyLimitMiddleware(app)(
            {"type": "http", "path": "/api/v1/video-requests", "method": "POST", "headers": []},
            receive,
            send,
        )
    )
    assert messages[0]["status"] == 413
    assert len(calls) == 2


def test_duplicate_forwarding_headers_use_complete_proxy_chain():
    assert key("10.0.0.2", ["203.0.113.99", "198.51.100.7"], "10.0.0.0/8") == key("198.51.100.7")


def test_supported_uvicorn_configuration_preserves_transport_peer():
    import uvicorn

    seen = []

    async def app(scope, receive, send):
        seen.append(scope["client"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    config = uvicorn.Config(app, proxy_headers=False, log_config=None)
    config.load()

    async def run():
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            pass

        await config.loaded_app(
            {
                "type": "http",
                "client": ("127.0.0.1", 1234),
                "headers": [(b"x-forwarded-for", b"198.51.100.8")],
            },
            receive,
            send,
        )

    asyncio.run(run())
    assert seen == [("127.0.0.1", 1234)]
