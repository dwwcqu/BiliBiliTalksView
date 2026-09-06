"""Header-only operator authentication and stable, privacy-preserving client identity."""

import hashlib
import hmac
import ipaddress
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .responses import APIError, response

bearer = HTTPBearer(auto_error=False)


def trusted_networks(value):
    values = value.split(",") if isinstance(value, str) else (value or [])
    try:
        return tuple(
            ipaddress.ip_network(item.strip(), strict=False) for item in values if item.strip()
        )
    except ValueError as exc:
        raise ValueError("invalid_trusted_proxies") from exc


def client_key(request: Request):
    secret = request.app.state.rate_limit_secret
    if not secret:
        raise APIError("rate_limit_not_configured", 503)
    peer = request.client.host if request.client else "unknown"
    networks = request.app.state.trusted_proxies

    def trusted(address):
        return any(
            address.version == network.version and address in network for network in networks
        )

    try:
        address = ipaddress.ip_address(peer)
        peer = str(address)
        fields = request.headers.getlist("x-forwarded-for")
        header = (
            ",".join(fields)
            if len(fields) <= 20 and sum(map(len, fields)) + max(0, len(fields) - 1) <= 2048
            else ""
        )
        if trusted(address) and header:
            chain = [ipaddress.ip_address(item.strip()) for item in header.split(",")]
            if len(chain) <= 20:
                for candidate in reversed(chain):
                    address = candidate
                    if not trusted(candidate):
                        break
                peer = str(address)
    except ValueError:
        pass  # Invalid chains are ignored; never accept a caller's arbitrary identity string.
    return hmac.new(
        secret.encode("utf-8"), ("api-v1-client:" + peer).encode("utf-8"), hashlib.sha256
    ).hexdigest()


def require_admin(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
):
    expected = request.app.state.admin_token
    if not expected:
        raise APIError("admin_not_configured", 503)
    if credentials is None or not hmac.compare_digest(
        credentials.credentials.encode("utf-8"), expected.encode("utf-8")
    ):
        raise APIError("unauthorized", 401)


class BodyLimitMiddleware:
    def __init__(self, app, limit=8192):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or not scope.get("path", "").startswith("/api/v1/")
            or scope["method"] not in {"POST", "PUT", "PATCH"}
        ):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.limit or int(declared) < 0:
                    await response({"error": "request_too_large"}, 413)(scope, receive, send)
                    return
            except ValueError:
                await response({"error": "invalid_request"}, 422)(scope, receive, send)
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.limit:
                await response({"error": "request_too_large"}, 413)(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)
