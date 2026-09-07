"""Pure projection layout helpers."""
import re


def order(record: dict) -> tuple:
    return record["created_at"] or "~", int(record["comment_id"])


def nickname(records: list[dict]) -> str | None:
    named = [r for r in records if r["author"]["nickname"]]
    if not named:
        return None
    return max(named, key=lambda r: (r["collected_at"], int(r["comment_id"])))["author"]["nickname"]


LATEST_SCHEMA_VERSION = "2.0.0"


def thread_directory(version: str) -> str:
    return "threads" if version.startswith("2.") else "楼内对话目录"


def user_directory(version: str) -> str:
    return "users" if version.startswith("2.") else "用户评论目录"


def user_folder(uid: str | None, name: str | None, version: str = "1.0.0") -> str:
    if version.startswith("2."):
        return "unknown" if uid is None else "uid_" + uid
    if uid is None:
        return "_unknown"
    label = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name or "昵称未知")[:32]
    label = re.sub(r"[\ud800-\udfff]", "_", label)
    label = re.sub(r"[ .]+$", lambda match: "_" * len(match[0]), label)
    return uid + "_" + label
