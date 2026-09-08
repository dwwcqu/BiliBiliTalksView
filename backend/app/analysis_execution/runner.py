"""Durable attempts for a trusted service; transport success is not acceptance."""

from datetime import UTC, datetime
from pathlib import Path

from app.analysis_input.locking import preparation_lock
from app.analysis_input.storage import require_ignored, safe_child
from app.analysis_packets.builder import sha
from app.analysis_packets.codec import json_bytes, loads
from app.analysis_results.acceptance import _write_once

from .command import CliRequest, build_command
from .events import parse_events
from .process import run_process


def execute(request: CliRequest, *, records_root: Path, environment: dict,
            authorized: bool = False) -> dict:
    if authorized is not True:
        raise ValueError("execution_not_authorized")
    if (not isinstance(environment, dict)
            or any(not isinstance(key, str) or not key or "=" in key or "\0" in key
                   or not isinstance(value, str) or "\0" in value
                   for key, value in environment.items())):
        raise ValueError("invalid_execution_environment")
    environment = dict(environment)
    argv = build_command(request)
    root = Path(records_root).absolute()
    require_ignored(root)
    # Check every component before creating the private session storage.
    root = safe_child(Path(root.anchor), root.relative_to(root.anchor).as_posix())
    session = safe_child(root, request.session_id)
    session.mkdir(parents=True, exist_ok=True)
    with preparation_lock(safe_child(session, "execution.lock")):
        attempt = safe_child(session, request.attempt_id)
        if attempt.exists():
            raise ValueError("attempt_already_started")
        for previous in session.iterdir():
            if not previous.is_dir():
                continue
            previous = safe_child(session, previous.name)
            receipt = safe_child(previous, "outcome.json")
            try:
                if not receipt.is_file():
                    raise ValueError("session_recovery_required")
                state = loads(receipt.read_text(encoding="utf-8"))
                if (not isinstance(state, dict)
                        or state.get("error_code") == "process_cleanup_unverified"):
                    raise ValueError("session_recovery_required")
            except (OSError, ValueError) as exc:
                raise ValueError("session_recovery_required") from exc
        attempt.mkdir()
        prompt = request.prompt.encode("utf-8")
        _write_once(attempt / "request.json", json_bytes({
            "session_id": request.session_id, "attempt_id": request.attempt_id,
            "created_at": datetime.now(UTC).isoformat(), "resume": request.resume,
            "configured_model": request.model, "executable": str(request.executable),
            "cwd": str(request.cwd), "prompt_sha256": sha(prompt),
            "prompt_bytes": len(prompt), "budget_usd": str(request.budget_usd),
            "timeout_seconds": request.timeout_seconds,
            "max_output_bytes": request.max_output_bytes,
            "agents_sha256": sha(json_bytes(request.agents)) if request.agents is not None else None,
            "cli_state_dir": environment.get("CLAUDE_CONFIG_DIR"),
            "create_agent": request.create_agent, "bare": request.bare,
            "direct_session": request.direct_session,
        }))
        captured = run_process(argv, cwd=request.cwd, env=environment, stdin=prompt,
                               timeout_seconds=request.timeout_seconds,
                               max_output_bytes=request.max_output_bytes)
        _write_once(attempt / "stdout.jsonl", captured["stdout"])
        _write_once(attempt / "stderr.txt", captured["stderr"])
        if captured["stop_reason"] is None:
            outcome = parse_events(captured["stdout"], session_id=request.session_id,
                                   returncode=captured["returncode"])
        else:
            outcome = {"status": "failed", "error_code": captured["stop_reason"],
                       "result": None, "estimated_cost_usd": None, "cost_status": "unknown"}
        outcome.update({
            "session_id": request.session_id, "attempt_id": request.attempt_id,
            "completed_at": datetime.now(UTC).isoformat(),
            "returncode": captured["returncode"],
            "trigger_reason": captured.get("trigger_reason"),
            "stdout_sha256": sha(captured["stdout"]),
            "stderr_sha256": sha(captured["stderr"]),
            "business_acceptance": "not_evaluated",
        })
        _write_once(attempt / "outcome.json", json_bytes(outcome))
        return outcome
