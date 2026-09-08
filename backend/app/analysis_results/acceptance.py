"""Accept local task responses only with explicit trusted execution evidence."""

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

from app.analysis_input.locking import preparation_lock
from app.analysis_input.storage import safe_child
from app.analysis_packets.builder import _measure, json_bytes, sha
from app.analysis_packets.codec import loads
from app.analysis_packets.contract import validate_document as validate_input
from app.analysis_packets.publication import (
    _check_history,
    _publish_path,
    _source_identity,
    read_published,
)
from app.analysis_packets.source import load_source
from app.analysis_packets.types import AcceptedResult

from .contract import schema_document, validate_document
from .errors import OutputError
from .types import ExecutionContext
from .validation import validate_result

PROTOCOL = "BiliBiliTalksView.AnalysisOutput"
VERSION = "1.0.0"


def bind_output_schema(assembly):
    """Bind new, unpublished task inputs; existing non-null references must agree."""
    result = deepcopy(assembly)
    raw = json_bytes(schema_document("task_result"))
    ref = {"path": "schemas/task-result.json", "sha256": sha(raw)}
    result.output_schemas = {ref["path"]: raw}
    loaded = b"".join(
        result.resource_files[result.resources[k]["path"]]
        for k in ("analysis_rules", "role_prompt", "coordination")
    )
    by_id = {}
    for packet in result.packets:
        old = packet["response_contract"]["schema_ref"]
        if old is not None and old != ref:
            raise OutputError("output_schema_conflict")
        packet["response_contract"]["schema_ref"] = dict(ref)
        _measure(packet, loaded)
        if packet["chunk"]["estimated_input_tokens"] > packet["chunk"]["input_token_limit"]:
            raise OutputError("input_budget_exceeded")
        by_id[packet["task_id"]] = packet
    for row in result.task_rows:
        row["input_sha256"] = sha(json_bytes(by_id[row["task_id"]]))
    return result


def _uuid(value):
    if not isinstance(value, str) or str(UUID(value)) != value:
        raise OutputError("invalid_identity")
    return value


def load_context(root, run_id, manifest_id):
    try:
        root = Path(root).resolve()
        _uuid(run_id)
        _uuid(manifest_id)
        candidates = [
            p / "runs" / run_id
            for p in root.iterdir()
            if p.name.startswith("bilibili-video-") and (p / "runs" / run_id).is_dir()
        ]
        if len(candidates) != 1:
            raise OutputError("run_not_found")
        run = safe_child(root, candidates[0].relative_to(root).as_posix())
        record = loads(safe_child(run, "run.json").read_text(encoding="utf-8"))
        manifest = loads(
            safe_child(run, f"manifests/{manifest_id}/run-manifest.json").read_text(
                encoding="utf-8"
            )
        )
        validate_input("manifest", manifest)
        if (
            manifest["run_id"] != run_id
            or manifest["manifest_id"] != manifest_id
            or record["run_id"] != run_id
        ):
            raise OutputError("run_identity_mismatch")
        bundle = load_source(root, manifest["prepared_run_id"], manifest["video_id"])
        _source_identity(manifest, bundle)
        packets = _check_history(run, manifest, bundle, record["first_manifest_id"])
        return run, bundle, packets, manifest
    except OutputError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise OutputError("invalid_run_context") from exc


def load_task_rows(run, manifest):
    from app.analysis_packets.publication import _lines, _read

    rows = {}
    seen = set()
    current = manifest
    while True:
        for row in _lines(_read(run, current["task_index"])):
            if row["task_id"] in rows and json_bytes(rows[row["task_id"]]) != json_bytes(row):
                raise OutputError("historical_task_conflict")
            rows[row["task_id"]] = row
        digest = current["previous_manifest_sha256"]
        if digest is None:
            return rows
        if digest in seen:
            raise OutputError("invalid_manifest_history")
        seen.add(digest)
        matching = []
        for path in safe_child(run, "manifests").glob("*/run-manifest.json"):
            path = safe_child(run, path.relative_to(run).as_posix())
            if sha(path.read_bytes()) == digest:
                matching.append(path)
        if len(matching) != 1:
            raise OutputError("invalid_manifest_history")
        current = loads(matching[0].read_text(encoding="utf-8"))


def _write_once(path: Path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise OutputError("result_conflict")
        return
    temporary = None
    try:
        with NamedTemporaryFile(prefix=".pending-", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
        _publish_path(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _read_response(raw):
    if not isinstance(raw, bytes) or raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
        raise OutputError("invalid_response_encoding")
    try:
        value = loads(raw.decode("utf-8"))
        validate_document("task_result", value)
        return value
    except OutputError:
        raise
    except (UnicodeError, ValueError, TypeError) as exc:
        raise OutputError("invalid_task_result") from exc


def _execution_binding(run, bundle, packet, manifest_id, execution):
    from app.analysis_packets.publication import _read

    if (
        not isinstance(execution, ExecutionContext)
        or execution.authorized is not True
        or execution.delivery_verified is not True
    ):
        raise OutputError("invalid_execution_evidence")
    ref = execution.execution_ref
    if not isinstance(ref, dict) or ref.get("path") != "execution.json":
        raise OutputError("execution_record_missing")
    config_raw = _read(run, ref)
    config = loads(config_raw.decode("utf-8"))
    for key in ("cli_version", "configured_model", "provider"):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise OutputError("invalid_execution_record")
    if (
        config.get("run_id") != packet["run_id"]
        or config.get("rules_sha256") != packet["resources"]["analysis_rules"]["sha256"]
        or config.get("input_protocol_version") != "2.0.0"
        or config.get("output_protocol_version") != VERSION
        or not isinstance(config.get("limits"), dict)
        or not isinstance(config.get("agent_ids"), list)
        or execution.agent_id not in config["agent_ids"]
    ):
        raise OutputError("execution_identity_mismatch")
    if (
        "reported_model" not in config
        or (config["reported_model"] is not None and not isinstance(config["reported_model"], str))
        or len(set(config["agent_ids"])) != len(config["agent_ids"])
    ):
        raise OutputError("invalid_execution_record")
    raw = safe_child(run, f"manifests/{_uuid(manifest_id)}/run-manifest.json").read_bytes()
    manifest = loads(raw.decode("utf-8"))
    registry = loads(_read(run, manifest["member_registry"]).decode("utf-8"))
    validate_input("member-registry", registry)
    members = [m for m in registry["members"] if m["agent_id"] == execution.agent_id]
    if (
        len(members) != 1
        or registry["run_id"] != packet["run_id"]
        or registry["video_id"] != bundle.video_id
        or registry["main"]["session_id"] != config.get("session_id")
    ):
        raise OutputError("unregistered_execution_agent")
    member = members[0]
    if (
        member["role"] != packet["task_type"]
        or packet["task_id"] not in member["task_ids"]
        or member["parent_session_id"] != config["session_id"]
        or member["role_sha256"] != packet["resources"]["role_prompt"]["sha256"]
        or member["status"] not in {"available", "running", "completed"}
    ):
        raise OutputError("unregistered_execution_agent")
    binding = {
        "run_id": packet["run_id"],
        "task_id": packet["task_id"],
        "attempt_id": execution.attempt_id,
        "input_sha256": execution.input_sha256,
        "rules_sha256": config["rules_sha256"],
        "execution_ref": ref,
        "agent_id": execution.agent_id,
        "manifest_id": manifest_id,
        "manifest_sha256": sha(raw),
        "authorized": True,
        "delivery_verified": True,
    }

    if execution.delivery_ref is not None:
        from app.analysis_execution.proof import verify_proof

        binding["delivery_result_sha256"] = verify_proof(
            run, execution.delivery_ref, packet=packet, execution=execution, config=config
        )
        binding["delivery_ref"] = execution.delivery_ref
        binding["label_catalog_sha256"] = config["label_catalog_sha256"]
    return binding


def _verify_binding(run, bundle, packet, receipt, rule_catalog):
    path = f"validation/{packet['task_id']}/{receipt['attempt_id']}/execution-binding.json"
    binding_raw = safe_child(run, path).read_bytes()
    if receipt.get("execution_binding_ref") != {"path": path, "sha256": sha(binding_raw)}:
        raise OutputError("execution_binding_changed")
    stored = loads(binding_raw.decode("utf-8"))
    execution = ExecutionContext(
        receipt["attempt_id"],
        receipt["input_sha256"],
        stored["authorized"],
        stored["delivery_verified"],
        stored["execution_ref"],
        stored["agent_id"],
        stored.get("delivery_ref"),
    )
    expected = _execution_binding(run, bundle, packet, stored["manifest_id"], execution)
    if (expected.get("label_catalog_sha256") is not None
            and expected["label_catalog_sha256"] != sha(json_bytes(rule_catalog))):
        raise OutputError("rule_catalog_mismatch")
    if (expected.get("delivery_result_sha256") is not None
            and expected["delivery_result_sha256"] != receipt["result_ref"]["sha256"]):
        raise OutputError("delivery_result_mismatch")
    if json_bytes(stored) != json_bytes(expected):
        raise OutputError("execution_binding_changed")
    return stored["manifest_id"]


def _load_catalog(run, bundle, packets, rule_catalog):
    result = {}
    for task_id, packet in packets.items():
        receipt_path = safe_child(run, f"validation/{task_id}/accepted.json")
        if not receipt_path.exists():
            continue
        receipt = loads(receipt_path.read_text(encoding="utf-8"))
        validate_document("validation_receipt", receipt)
        if (
            receipt["decision"] != "accepted"
            or receipt["task_id"] != task_id
            or receipt["run_id"] != packet["run_id"]
        ):
            raise OutputError("invalid_acceptance_receipt")
        expected = f"results/{task_id}/{receipt['attempt_id']}.json"
        if receipt["result_ref"] is None or receipt["result_ref"]["path"] != expected:
            raise OutputError("invalid_acceptance_receipt")
        raw = safe_child(run, expected).read_bytes()
        if sha(raw) != receipt["result_ref"]["sha256"]:
            raise OutputError("accepted_result_changed")
        packet_raw = safe_child(run, f"tasks/{task_id}/input.json").read_bytes()
        if sha(packet_raw) != receipt["input_sha256"]:
            raise OutputError("accepted_input_changed")
        _verify_binding(run, bundle, packet, receipt, rule_catalog)
        schema_ref = packet["response_contract"]["schema_ref"]
        if (
            schema_ref is None
            or sha(safe_child(run, schema_ref["path"]).read_bytes()) != schema_ref["sha256"]
        ):
            raise OutputError("output_schema_mismatch")
        document = _read_response(raw)
        validate_result(
            document,
            packet,
            rule_catalog,
            attempt_id=receipt["attempt_id"],
            input_sha256=sha(packet_raw),
        )
        if document["model_status"] != "completed":
            raise OutputError("incomplete_result_not_accepted")
        result[task_id] = AcceptedResult(packet, expected, raw, tuple(document["observations"]))
    return result


def load_catalog(root, run_id, manifest_id, rule_catalog):
    try:
        run, bundle, packets, _ = load_context(root, run_id, manifest_id)
        catalog = _load_catalog(run, bundle, packets, rule_catalog)
        validated = {manifest_id}
        read_published(root, run_id, manifest_id, catalog)
        for task_id in catalog:
            receipt = loads(
                safe_child(run, f"validation/{task_id}/accepted.json").read_text(encoding="utf-8")
            )
            binding = loads(
                safe_child(
                    run, f"validation/{task_id}/{receipt['attempt_id']}/execution-binding.json"
                ).read_text(encoding="utf-8")
            )
            if binding["manifest_id"] not in validated:
                read_published(root, run_id, binding["manifest_id"], catalog)
                validated.add(binding["manifest_id"])
        return catalog
    except OutputError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise OutputError("invalid_accepted_catalog") from exc


def read_state(root, run_id, manifest_id, rule_catalog):
    catalog = load_catalog(root, run_id, manifest_id, rule_catalog)
    assembly, bundle = read_published(root, run_id, manifest_id, catalog)
    run, _, _, _ = load_context(root, run_id, manifest_id)
    return run, bundle, assembly, catalog


def accept_response(root, run_id, manifest_id, task_id, raw, *, execution, rule_catalog):
    if not isinstance(execution, ExecutionContext) or not isinstance(raw, bytes):
        raise OutputError("invalid_execution_context")
    _uuid(task_id)
    _uuid(execution.attempt_id)
    run, bundle, packets, _ = load_context(root, run_id, manifest_id)
    if task_id not in packets:
        raise OutputError("unpublished_task")
    packet = packets[task_id]
    with preparation_lock(safe_child(run, ".results.lock")):
        decision, reason = "rejected", []
        ref = None
        binding_ref = None
        document = None
        try:
            if execution.authorized is not True:
                raise OutputError("execution_not_authorized")
            if execution.delivery_verified is not True:
                raise OutputError("input_delivery_unverified")
            packet_raw = safe_child(run, f"tasks/{task_id}/input.json").read_bytes()
            if sha(packet_raw) != execution.input_sha256:
                raise OutputError("input_hash_mismatch")
            schema_ref = packet["response_contract"]["schema_ref"]
            if schema_ref is None:
                raise OutputError("output_contract_missing")
            schema_raw = safe_child(run, schema_ref["path"]).read_bytes()
            if sha(schema_raw) != schema_ref["sha256"] or loads(
                schema_raw.decode("utf-8")
            ) != schema_document("task_result"):
                raise OutputError("output_schema_mismatch")
            binding = _execution_binding(run, bundle, packet, manifest_id, execution)
            if (binding.get("label_catalog_sha256") is not None
                    and binding["label_catalog_sha256"] != sha(json_bytes(rule_catalog))):
                raise OutputError("rule_catalog_mismatch")
            if (binding.get("delivery_result_sha256") is not None
                    and binding["delivery_result_sha256"] != sha(raw)):
                raise OutputError("delivery_result_mismatch")
            catalog = load_catalog(root, run_id, manifest_id, rule_catalog)
            read_published(root, run_id, manifest_id, catalog)
            if task_id in catalog:
                old = loads(
                    safe_child(run, f"validation/{task_id}/accepted.json").read_text(
                        encoding="utf-8"
                    )
                )
                if (
                    old["attempt_id"] == execution.attempt_id
                    and catalog[task_id].result_bytes == raw
                ):
                    return old
                raise OutputError(
                    "already_accepted"
                    if old["attempt_id"] != execution.attempt_id
                    else "result_conflict"
                )
            document = _read_response(raw)
            validate_result(
                document,
                packet,
                rule_catalog,
                attempt_id=execution.attempt_id,
                input_sha256=execution.input_sha256,
            )
            decision = "accepted" if document["model_status"] == "completed" else "incomplete"
            if decision == "accepted":
                path = f"results/{task_id}/{execution.attempt_id}.json"
                _write_once(safe_child(run, path), raw)
                ref = {"path": path, "sha256": sha(raw)}
                binding_path = f"validation/{task_id}/{execution.attempt_id}/execution-binding.json"
                binding_raw = json_bytes(binding)
                _write_once(safe_child(run, binding_path), binding_raw)
                binding_ref = {"path": binding_path, "sha256": sha(binding_raw)}
            else:
                reason = ["task_incomplete"]
        except OutputError as exc:
            decision, reason = "rejected", [exc.code]
        except (OSError, ValueError, KeyError, TypeError):
            decision, reason = "rejected", ["acceptance_validation_failed"]
        receipt = {
            "protocol": PROTOCOL,
            "schema_version": VERSION,
            "artifact_type": "validation_receipt",
            "task_id": task_id,
            "run_id": run_id,
            "attempt_id": execution.attempt_id,
            "input_sha256": execution.input_sha256,
            "decision": decision,
            "reason_codes": reason,
            "validated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "result_ref": ref,
            "execution_binding_ref": binding_ref,
        }
        validate_document("validation_receipt", receipt)
        if decision == "accepted":
            _write_once(safe_child(run, f"validation/{task_id}/accepted.json"), json_bytes(receipt))
        else:
            directory = f"intermediate/tasks/{task_id}/{execution.attempt_id}"
            response_path = safe_child(run, directory + "/response.txt")
            receipt_path = safe_child(run, directory + "/receipt.json")
            _write_once(response_path, raw)
            if receipt_path.exists():
                previous = loads(receipt_path.read_text(encoding="utf-8"))
                if {k: v for k, v in previous.items() if k != "validated_at"} == {
                    k: v for k, v in receipt.items() if k != "validated_at"
                }:
                    return previous
            _write_once(receipt_path, json_bytes(receipt))
        return receipt
