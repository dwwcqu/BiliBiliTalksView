"""Consume a complete exported batch before downstream side effects."""
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from app.storage.errors import StorageError
from app.storage.frozen import freeze_batch

from .digest import exact_digest
from .errors import InputPreparationError


def independent_roots(source: Path, destination: Path) -> tuple[Path, Path]:
    source, destination = source.resolve(), destination.resolve()
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise InputPreparationError("invalid_storage_root")
    return source, destination


def validate_encoding(directory: Path) -> None:
    for path in directory.rglob("*"):
        if path.is_symlink() or path.is_junction():
            raise InputPreparationError("invalid_export")
        if not path.is_file():
            continue
        raw = path.read_bytes()
        raw.decode("utf-8")
        if path.name != "README.md" and (raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw):
            raise InputPreparationError("invalid_export")


def stage_current(container: Path, staging: Path) -> dict:
    container, staging = independent_roots(Path(container), Path(staging))
    if staging.exists():
        raise InputPreparationError("staging_exists")
    staging.parent.mkdir(parents=True, exist_ok=True)
    try:
        with (
            TemporaryDirectory(prefix="freeze-input-", dir=staging.parent) as work,
            freeze_batch(container / "current.json", Path(work), analysis_readme=True,
                         digest_function=exact_digest) as frozen,
        ):
            validate_encoding(frozen.directory)
            # Same filesystem: preserve validated bytes, not a live source reference.
            frozen.directory.rename(staging)
            return frozen.manifest
    except InputPreparationError:
        raise
    except StorageError as exc:
        raise InputPreparationError(str(exc)) from exc
    except (ValueError, UnicodeError) as exc:
        raise InputPreparationError("invalid_export") from exc
    except OSError as exc:
        if staging.exists():
            shutil.rmtree(staging)
        raise InputPreparationError("storage_error") from exc
