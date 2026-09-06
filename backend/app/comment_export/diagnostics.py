"""Safe request diagnostics; full opaque cursors stay in protected task storage."""

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from uuid import UUID, uuid4

ENDPOINT_PHASES = {
    "/pgc/view/web/season": "resolve",
    "/x/web-interface/view": "resolve",
    "/x/web-interface/nav": "signing_keys",
    "/x/v2/reply/wbi/main": "main",
    "/x/v2/reply/reply": "replies",
}


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_retry_after(value: str | None, observed_at: str | datetime) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        observed = (
            datetime.fromisoformat(observed_at) if isinstance(observed_at, str) else observed_at
        )
        if observed.tzinfo is None:
            return None
        if re.fullmatch(r"[0-9]+", value.strip()):
            result = observed + timedelta(seconds=int(value.strip()))
        else:
            result = parsedate_to_datetime(value)
            if result.tzinfo is None:
                return None
        return result.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, OverflowError):
        return None


def classify_failure(http_status: int | None, api_code: int | None, endpoint: str) -> str:
    if http_status is None:
        return "network_error"
    if http_status == 429:
        return "rate_limited"
    if http_status == 401:
        return "authentication_required"
    if http_status in (403, 412):
        return "access_restricted"
    if 500 <= http_status <= 599:
        return "source_unavailable"
    if http_status != 200:
        return "request_rejected"
    if endpoint == "/x/web-interface/nav" and api_code == -101:
        return "authentication_required"
    if endpoint == "/x/v2/reply/reply" and api_code in {12006, 12022}:
        return "resource_unavailable"
    if api_code is not None and api_code != 0:
        return "access_restricted"
    return "invalid_response"


@dataclass(frozen=True)
class FailureDetail:
    failure_id: str = field(default_factory=lambda: str(uuid4()))
    observed_at: str = field(default_factory=utc_now)
    phase: str = "local_validation"
    endpoint: str | None = None
    http_status: int | None = None
    api_code: int | None = None
    category: str = "local_validation"
    video_id: str | None = None
    target: dict = field(default_factory=dict)
    retry_after_at: str | None = None
    checkpoint_revision: int = 0
    safe_reason: str = "unknown_failure"

    def __post_init__(self) -> None:
        phases = {"resolve", "signing_keys", "main", "replies", "login_check", "local_validation"}
        categories = {
            "network_error",
            "source_unavailable",
            "authentication_required",
            "rate_limited",
            "access_restricted",
            "request_rejected",
            "invalid_response",
            "local_validation",
            "unknown_legacy",
            "resource_unavailable",
        }
        if not isinstance(self.phase, str) or self.phase not in phases:
            raise ValueError("invalid_diagnostic_phase")
        if not isinstance(self.category, str) or self.category not in categories:
            raise ValueError("invalid_diagnostic_category")
        try:
            if (
                not isinstance(self.failure_id, str)
                or str(UUID(self.failure_id)) != self.failure_id
            ):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ValueError("invalid_diagnostic_failure_id") from None
        for name, value in (
            ("observed_at", self.observed_at),
            ("retry_after_at", self.retry_after_at),
        ):
            if name == "retry_after_at" and value is None:
                continue
            try:
                if not isinstance(value, str) or not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value
                ):
                    raise ValueError
                datetime.fromisoformat(value)
            except (ValueError, TypeError):
                raise ValueError("invalid_diagnostic_" + name) from None
        if self.http_status is not None and (
            type(self.http_status) is not int or not 100 <= self.http_status <= 599
        ):
            raise ValueError("invalid_diagnostic_http_status")
        if self.api_code is not None and type(self.api_code) is not int:
            raise ValueError("invalid_diagnostic_api_code")
        if type(self.checkpoint_revision) is not int or self.checkpoint_revision < 0:
            raise ValueError("invalid_diagnostic_checkpoint_revision")
        if self.video_id is not None and (
            not isinstance(self.video_id, str)
            or not re.fullmatch(r"bilibili:video:[1-9][0-9]*", self.video_id)
        ):
            raise ValueError("invalid_diagnostic_video_id")
        if self.endpoint is not None and (
            not isinstance(self.endpoint, str) or self.endpoint not in ENDPOINT_PHASES
        ):
            raise ValueError("invalid_diagnostic_endpoint")
        if not isinstance(self.target, dict) or set(self.target) - {
            "cursor",
            "root_id",
            "page",
            "bvid",
            "episode_id",
            "aid",
        }:
            raise ValueError("invalid_diagnostic_target")
        for name, value in self.target.items():
            if name == "page":
                valid = type(value) is int and value > 0
            elif name == "cursor":
                valid = isinstance(value, str)
            elif name == "bvid":
                valid = isinstance(value, str) and re.fullmatch(r"BV[0-9A-Za-z]{10}", value)
            else:
                valid = isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value)
            if not valid:
                raise ValueError("invalid_diagnostic_target")
        if not isinstance(self.safe_reason, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]*", self.safe_reason
        ):
            raise ValueError("invalid_diagnostic_safe_reason")

    def to_dict(self) -> dict:
        return asdict(self)

    def to_public_dict(self) -> dict:
        result = self.to_dict()
        cursor = result["target"].pop("cursor", None)
        if cursor is not None:
            result["target"]["cursor_digest"] = hashlib.sha256(
                cursor.encode("utf-8", errors="surrogatepass")
            ).hexdigest()
        return result


def is_reply_unavailable(detail) -> bool:
    value = detail.to_dict() if hasattr(detail, "to_dict") else detail
    return (isinstance(value, dict) and value.get("endpoint") == "/x/v2/reply/reply"
            and value.get("http_status") == 200 and value.get("api_code") in {12006, 12022})
