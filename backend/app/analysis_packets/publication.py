"""Publish immutable offline assemblies and validate them by explicit manifest ID."""

import re
import time
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from app.analysis_input.locking import preparation_lock
from app.analysis_input.storage import require_ignored, safe_child

from .builder import PROTOCOL, VERSION, json_bytes, jsonl_bytes, sha
from .codec import loads as parse_json
from .contract import validate_document
from .errors import PacketError
from .source import load_source
from .types import Assembly


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise PacketError("invalid_identity")
    return value


def _ref(path, data):
    return {"path": path, "sha256": sha(data)}


def _read(root, ref):
    data = safe_child(root, ref["path"]).read_bytes()
    if sha(data) != ref["sha256"]:
        raise PacketError("reference_hash_mismatch")
    return data


def _lines(data):
    if (data and not data.endswith(b"\n")) or b"\r" in data or data.startswith(b"\xef\xbb\xbf"):
        raise PacketError("invalid_jsonl")
    return [parse_json(line) for line in data.decode("utf-8").split("\n")[:-1]]


def _check_file_paths(files):
    aliases = set()
    allowed = {
        "context",
        "tasks",
        "indexes",
        "registries",
        "manifests",
        "results",
        "intermediate",
        "schemas",
    }
    for name in files:
        parts = name.split("/") if isinstance(name, str) else []
        if not parts or parts[0] not in allowed:
            raise PacketError("invalid_output_path")
        for part in parts:
            if (
                not part
                or part in {".", ".."}
                or part[-1] in ". "
                or any(c in part for c in '\\:<>"|?*')
                or any(ord(c) < 32 for c in part)
                or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])", part.split(".")[0])
            ):
                raise PacketError("invalid_output_path")
        alias = name.casefold()
        if alias in aliases:
            raise PacketError("output_path_collision")
        aliases.add(alias)


def _source_identity(manifest, bundle):
    for key, expected in (
        ("video_id", bundle.video_id),
        ("export_id", bundle.export_id),
        ("export_schema_version", bundle.export_schema_version),
        ("prepared_run_id", bundle.prepared_run_id),
        ("input_fingerprint", bundle.input_fingerprint),
    ):
        if manifest[key] != expected:
            raise PacketError("manifest_source_mismatch")
    if json_bytes(manifest["coverage"]["source"]) != json_bytes(
        bundle.manifest["coverage"]
    ) or json_bytes(manifest["coverage"]["corpus_counts"]) != json_bytes(bundle.manifest["counts"]):
        raise PacketError("manifest_source_mismatch")


def _check_history(run, manifest, bundle, first_manifest_id):
    seen = set()
    packets = {}
    current = manifest
    while True:
        task_rows = _lines(_read(run, current["task_index"]))
        if len(task_rows) != current["task_index"]["task_count"]:
            raise PacketError("task_count_mismatch")
        for row in task_rows:
            validate_document("task-index-row", row)
            packet = parse_json(
                _read(run, {"path": row["input_path"], "sha256": row["input_sha256"]}).decode(
                    "utf-8"
                )
            )
            validate_document("packet", packet)
            if packet["task_id"] != row["task_id"] or packet["run_id"] != manifest["run_id"]:
                raise PacketError("invalid_manifest_history")
            if row["task_id"] in packets and json_bytes(packets[row["task_id"]]) != json_bytes(
                packet
            ):
                raise PacketError("immutable_file_conflict")
            packets[row["task_id"]] = packet
        digest = current["previous_manifest_sha256"]
        if digest is None:
            if current["manifest_id"] != first_manifest_id:
                raise PacketError("invalid_manifest_history")
            return packets
        if digest in seen:
            raise PacketError("invalid_manifest_history")
        seen.add(digest)
        matches = []
        for path in safe_child(run, "manifests").glob("*/run-manifest.json"):
            safe = safe_child(run, path.relative_to(run).as_posix())
            if sha(safe.read_bytes()) == digest:
                matches.append(safe)
        if len(matches) != 1:
            raise PacketError("invalid_manifest_history")
        previous = parse_json(matches[0].read_text(encoding="utf-8"))
        validate_document("manifest", previous)
        _source_identity(previous, bundle)
        if (
            previous["manifest_id"] != matches[0].parent.name
            or previous["run_id"] != manifest["run_id"]
            or previous["resources"] != manifest["resources"]
        ):
            raise PacketError("invalid_manifest_history")
        current = previous


def _verify_dependencies(assembly, accepted, historical):
    known = dict(historical)
    for packet in assembly.packets:
        if packet["task_id"] in known and json_bytes(known[packet["task_id"]]) != json_bytes(
            packet
        ):
            raise PacketError("immutable_file_conflict")
        known[packet["task_id"]] = packet
    used = {task for row in assembly.task_rows for task in row["depends_on"]}
    for task in used:
        if task not in known:
            raise PacketError("unpublished_dependency")
        if task in accepted and json_bytes(accepted[task].packet) != json_bytes(known[task]):
            raise PacketError("result_source_packet_mismatch")
    return used


def _publish_path(source, target):
    for attempt in range(5):
        if target.exists():
            raise PacketError("immutable_file_conflict")
        try:
            source.rename(target)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 4:
                raise
            time.sleep((0.1, 0.3, 0.8, 1.5)[attempt])


def publish_assembly(analysis_root, assembly, bundle, accepted=None):
    from .validation import validate_assembly

    accepted = accepted or {}
    try:
        _uuid(assembly.run_id)
        validate_assembly(assembly, bundle, accepted)
        root = Path(analysis_root).resolve()
        require_ignored(root)
        video = safe_child(root, "bilibili-video-" + bundle.video_id.rsplit(":", 1)[1])
        if not video.is_dir():
            raise PacketError("prepared_video_missing")
        run = safe_child(video, "runs/" + assembly.run_id)
        manifest_id = str(uuid4())
        prefix = "manifests/" + manifest_id
        files = dict(assembly.resource_files)

        def put(path, data):
            if path in files and files[path] != data:
                raise PacketError("file_reference_conflict")
            files[path] = data

        for path, data in assembly.output_schemas.items():
            put(path, data)
        for packet in assembly.packets:
            put(f"tasks/{packet['task_id']}/input.json", json_bytes(packet))
        for path, rows in assembly.target_indexes.items():
            put(path, jsonl_bytes(rows))
        used_results = {task for row in assembly.task_rows for task in row["depends_on"]}
        for task in used_results:
            if task in accepted:
                result = accepted[task]
                put(result.result_path, result.result_bytes)
        registry_path = f"registries/{assembly.member_registry['registry_id']}.json"
        put(registry_path, json_bytes(assembly.member_registry))
        group_path, task_path = prefix + "/group-coverage.json", prefix + "/task-index.jsonl"
        put(group_path, json_bytes(assembly.group_coverage))
        put(task_path, jsonl_bytes(assembly.task_rows))
        manifest = {
            "protocol": PROTOCOL,
            "schema_version": VERSION,
            "manifest_id": manifest_id,
            "previous_manifest_sha256": assembly.previous_manifest_sha256,
            "run_id": assembly.run_id,
            "prepared_run_id": bundle.prepared_run_id,
            "video_id": bundle.video_id,
            "export_id": bundle.export_id,
            "export_schema_version": bundle.export_schema_version,
            "input_fingerprint": bundle.input_fingerprint,
            "resources": assembly.resources,
            "coverage": {
                "source": bundle.manifest["coverage"],
                "corpus_counts": bundle.manifest["counts"],
                "context_gaps": [],
                "omitted_context_ids": [],
                "summary_used": False,
                "limitations": ["Offline assembly; no model was called."],
            },
            "task_index": _ref(task_path, files[task_path])
            | {"task_count": len(assembly.task_rows)},
            "member_registry": _ref(registry_path, files[registry_path]),
            "group_coverage": _ref(group_path, files[group_path]),
            "execution_limits": assembly.limits,
        }
        validate_document("manifest", manifest)
        manifest_path = prefix + "/run-manifest.json"
        put(manifest_path, json_bytes(manifest))
        _check_file_paths(files)
        with preparation_lock(safe_child(video, ".packets.lock")):
            initial = not run.exists()
            history_packets = {}
            if initial and assembly.previous_manifest_sha256 is not None:
                raise PacketError("previous_manifest_missing")
            if not initial:
                record = parse_json(safe_child(run, "run.json").read_text(encoding="utf-8"))
                if (
                    record.get("run_id") != assembly.run_id
                    or record.get("prepared_run_id") != bundle.prepared_run_id
                ):
                    raise PacketError("run_identity_mismatch")
                prior = [
                    p
                    for p in safe_child(run, "manifests").glob("*/run-manifest.json")
                    if sha(p.read_bytes()) == assembly.previous_manifest_sha256
                ]
                if len(prior) != 1:
                    raise PacketError("previous_manifest_missing")
                prior_path = safe_child(run, prior[0].relative_to(run).as_posix())
                before = parse_json(prior_path.read_text(encoding="utf-8"))
                _source_identity(before, bundle)
                history_packets = _check_history(run, before, bundle, record["first_manifest_id"])
                for key in ("run_id", "video_id", "export_id", "prepared_run_id", "resources"):
                    if before[key] != manifest[key]:
                        raise PacketError("run_identity_mismatch")
            _verify_dependencies(assembly, accepted, history_packets)
            with TemporaryDirectory(prefix=".packets-", dir=video) as temporary:
                staged = Path(temporary) / "run"
                staged.mkdir()
                for path, data in files.items():
                    dest = safe_child(staged, path)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    if not initial:
                        existing = safe_child(run, path)
                        if existing.exists() and existing.read_bytes() != data:
                            raise PacketError("immutable_file_conflict")
                if initial:
                    for folder in ("intermediate", "users"):
                        (staged / folder).mkdir()
                    (staged / "run.json").write_bytes(
                        json_bytes(
                            {
                                "status": "offline_prepared",
                                "run_id": assembly.run_id,
                                "prepared_run_id": bundle.prepared_run_id,
                                "video_id": bundle.video_id,
                                "context_window": assembly.context_window,
                                "first_manifest_id": manifest_id,
                                "model_execution_authorized": False,
                                "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                            }
                        )
                    )
                    _publish_path(staged, run)
                else:
                    # Unreferenced files can be left by a failed attempt; only the final
                    # manifest directory rename publishes this new immutable snapshot.
                    for path in files:
                        if path.startswith(prefix + "/"):
                            continue
                        dest = safe_child(run, path)
                        if not dest.exists():
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            _publish_path(safe_child(staged, path), dest)
                    safe_child(run, "manifests").mkdir(exist_ok=True)
                    _publish_path(safe_child(staged, prefix), safe_child(run, prefix))
        return {
            "status": "offline_prepared",
            "run_id": assembly.run_id,
            "manifest_id": manifest_id,
            "manifest_sha256": sha(files[manifest_path]),
            "run_path": str(run),
            "manifest_path": str(run / manifest_path),
            "task_count": len(assembly.task_rows),
            "model_execution_authorized": False,
        }
    except PacketError:
        raise
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise PacketError("packet_publication_failed") from exc


def read_published(analysis_root, run_id, manifest_id, accepted=None):
    from .validation import validate_assembly

    try:
        root = Path(analysis_root).resolve()
        _uuid(run_id)
        _uuid(manifest_id)
        candidates = [
            p / "runs" / run_id
            for p in root.iterdir()
            if p.name.startswith("bilibili-video-") and (p / "runs" / run_id).is_dir()
        ]
        if len(candidates) != 1:
            raise PacketError("run_not_found")
        run = safe_child(root, candidates[0].relative_to(root).as_posix())
        record = parse_json(safe_child(run, "run.json").read_text(encoding="utf-8"))
        manifest = parse_json(
            safe_child(run, f"manifests/{manifest_id}/run-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        validate_document("manifest", manifest)
        if (
            manifest["run_id"] != run_id
            or manifest["manifest_id"] != manifest_id
            or record["run_id"] != run_id
            or record["video_id"] != manifest["video_id"]
            or record["prepared_run_id"] != manifest["prepared_run_id"]
            or record["model_execution_authorized"] is not False
        ):
            raise PacketError("run_identity_mismatch")
        bundle = load_source(root, manifest["prepared_run_id"], manifest["video_id"])
        _source_identity(manifest, bundle)
        history_packets = _check_history(run, manifest, bundle, record["first_manifest_id"])
        resources = manifest["resources"]
        resource_files = {value["path"]: _read(run, value) for value in resources.values()}
        task_rows = _lines(_read(run, manifest["task_index"]))
        if len(task_rows) != manifest["task_index"]["task_count"]:
            raise PacketError("task_count_mismatch")
        packets = [
            parse_json(
                _read(run, {"path": row["input_path"], "sha256": row["input_sha256"]}).decode(
                    "utf-8"
                )
            )
            for row in task_rows
        ]
        groups = parse_json(_read(run, manifest["group_coverage"]).decode("utf-8"))
        indexes = {
            g["target_index"]["path"]: _lines(_read(run, g["target_index"]))
            for g in groups["groups"]
        }
        registry = parse_json(_read(run, manifest["member_registry"]).decode("utf-8"))
        assembly = Assembly(
            run_id,
            packets,
            task_rows,
            indexes,
            groups,
            resources,
            resource_files,
            registry,
            manifest["execution_limits"],
            record["context_window"],
            manifest["previous_manifest_sha256"],
        )
        accepted = accepted or {}
        used_results = _verify_dependencies(assembly, accepted, history_packets)
        for task in used_results:
            if task in accepted:
                result = accepted[task]
                _read(run, {"path": result.result_path, "sha256": sha(result.result_bytes)})
        schema_refs = [
            p["response_contract"]["schema_ref"]
            for p in packets
            if p["response_contract"]["schema_ref"] is not None
        ]
        assembly.output_schemas = {ref["path"]: _read(run, ref) for ref in schema_refs}
        validate_assembly(assembly, bundle, accepted)
        if manifest["input_fingerprint"] != bundle.input_fingerprint:
            raise PacketError("input_fingerprint_mismatch")
        return assembly, bundle
    except PacketError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PacketError("invalid_published_assembly") from exc


def validate_published(analysis_root, run_id, manifest_id, accepted=None):
    assembly, _ = read_published(analysis_root, run_id, manifest_id, accepted)
    return {
        "status": "valid_offline",
        "run_id": run_id,
        "manifest_id": manifest_id,
        "task_count": len(assembly.packets),
        "model_execution_authorized": False,
    }
