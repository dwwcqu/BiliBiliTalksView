"""Precision-preserving analysis identity, separate from existing DB receipt digests."""
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from app.comment_export.validation import safe_path

ALGORITHM = "analysis-exact-json-v1"


def tagged(value):
    if value is None or isinstance(value, (str, bool)):
        return [type(value).__name__, value]
    if isinstance(value, (int, Decimal)):
        number = Decimal(value)
        if not number.is_finite():
            raise ValueError("nonfinite_json_number")
        sign, digits, exponent = number.as_tuple()
        digits = list(digits)
        while digits and digits[-1] == 0:
            digits.pop()
            exponent += 1
        return ["number", sign if digits else 0, "".join(map(str, digits)),
                exponent if digits else 0]
    if isinstance(value, list):
        return ["array", [tagged(item) for item in value]]
    return ["object", [[key, tagged(value[key])] for key in sorted(value)]]


def exact_digest(directory: Path) -> str:
    # Call only after strict schema / duplicate-key validation; never rewrite originals.
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    projections = []
    for group in ("threads", "users"):
        records = {}
        for entry in manifest[group]:
            info = json.loads(safe_path(directory, entry["path"]).read_text(encoding="utf-8"))
            text = safe_path(directory, info["comments_path"]).read_text(encoding="utf-8")
            for line in text.split("\n")[:-1]:
                row = json.loads(line, parse_float=Decimal)
                records[row["comment_id"]] = tagged(row)
        projections.append(records)
    if projections[0] != projections[1]:
        raise ValueError("precise_projection_mismatch")
    entries = []
    for path in sorted(directory.rglob("*"), key=lambda p: p.relative_to(directory).as_posix()):
        if not path.is_file() or path.relative_to(directory).as_posix() == "README.md":
            continue
        text = path.read_text(encoding="utf-8")
        value = ([json.loads(line, parse_float=Decimal) for line in text.split("\n")[:-1]]
                 if path.suffix == ".jsonl" else json.loads(text, parse_float=Decimal))
        entries.append([path.relative_to(directory).as_posix(), tagged(value)])
    raw = json.dumps(entries, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("ascii")).hexdigest()
