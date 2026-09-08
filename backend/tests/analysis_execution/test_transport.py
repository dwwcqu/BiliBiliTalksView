import sys
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from app.analysis_execution.command import CliRequest, build_command
from app.analysis_execution.process import run_process


def request(tmp_path):
    return CliRequest(
        executable=Path(sys.executable), cwd=tmp_path, session_id=str(uuid4()),
        attempt_id=str(uuid4()), model="synthetic-model", prompt="中文 $(secret) & text",
        budget_usd=Decimal("0.25"), timeout_seconds=5, max_output_bytes=65536,
        resume=True,
    )


def test_resume_uses_exact_session_and_stdin_not_shell_arguments(tmp_path):
    req = request(tmp_path)
    argv = build_command(req)
    assert argv[argv.index("--resume") + 1] == req.session_id
    assert "--session-id" not in argv
    assert req.prompt not in argv
    assert argv[argv.index("--max-budget-usd") + 1] == "0.25"
    assert "--bare" in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "--forward-subagent-text" in argv


def test_new_session_never_forks_or_continues_recent_session(tmp_path):
    req = replace(request(tmp_path), resume=False)
    argv = build_command(req)
    assert argv[argv.index("--session-id") + 1] == req.session_id
    assert not {"--resume", "--continue", "--fork-session"}.intersection(argv)


@pytest.mark.parametrize("field,value", [
    ("budget_usd", Decimal("NaN")), ("budget_usd", Decimal(0)),
    ("budget_usd", True), ("timeout_seconds", float("inf")),
    ("timeout_seconds", 0), ("max_output_bytes", -1), ("max_output_bytes", True),
    ("session_id", "../../escape"), ("attempt_id", "bad"), ("model", ""),
    ("model", "--bad"), ("resume", "false"), ("prompt", ""),
    ("prompt", "x" * (10 * 1024 * 1024 + 1)),
], ids=lambda value: "large-prompt" if isinstance(value, str) and len(value) > 100 else None)
def test_invalid_request_rejected_before_launch(tmp_path, field, value):
    with pytest.raises(ValueError):
        build_command(replace(request(tmp_path), **{field: value}))


def process(tmp_path, code, **overrides):
    args = {"cwd": tmp_path, "env": {}, "stdin": "中文 & $()".encode(),
                "timeout_seconds": 5, "max_output_bytes": 65536}
    args.update(overrides)
    return run_process([sys.executable, "-I", "-c", code], **args)


def test_process_preserves_utf8_stdin_and_nonzero_exit(tmp_path):
    got = process(tmp_path,
                  "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); "
                  "sys.stderr.write('failure'); sys.exit(3)")
    assert got["stdout"] == "中文 & $()".encode()
    assert got["stderr"] == b"failure"
    assert got["returncode"] == 3
    assert got["stop_reason"] is None


def test_timeout_stops_process_and_retains_partial_output(tmp_path):
    got = process(tmp_path, "import time; print('partial', flush=True); time.sleep(20)",
                  timeout_seconds=0.4)
    assert got["stop_reason"] == "timeout"
    assert b"partial" in got["stdout"]
    assert got["returncode"] != 0


def test_output_limit_bounds_captured_data(tmp_path):
    got = process(tmp_path, "import sys,time; sys.stdout.write('x'*200000); "
                  "sys.stdout.flush(); time.sleep(20)", max_output_bytes=4096)
    assert got["trigger_reason"] == "output_limit"
    assert got["stop_reason"] in {"output_limit", "process_cleanup_unverified"}
    assert len(got["stdout"]) + len(got["stderr"]) <= 4096


def test_unauthorized_does_not_create_attempt_or_spawn(tmp_path):
    from app.analysis_execution.runner import execute
    req = request(tmp_path)
    root = tmp_path / "records"
    with pytest.raises(ValueError, match="execution_not_authorized"):
        execute(req, records_root=root, environment={})
    assert not root.exists()


def test_failed_spawn_is_recorded_and_same_attempt_never_retries(tmp_path):
    import json

    from app.analysis_execution.runner import execute
    req = request(tmp_path)
    # Existing file that is not executable: real OS spawn failure, no paid process.
    bad = tmp_path / "not-executable.exe"
    bad.write_bytes(b"invalid executable")
    req = replace(req, executable=bad)
    root = tmp_path / "records"
    outcome = execute(req, records_root=root, environment={"TEST_SECRET": "never-save"},
                      authorized=True)
    assert outcome["status"] == "failed"
    assert outcome["error_code"] == "spawn_failed"
    directory = root / req.session_id / req.attempt_id
    assert json.loads((directory / "outcome.json").read_text())["cost_status"] == "unknown"
    with pytest.raises(ValueError, match="attempt_already_started"):
        execute(req, records_root=root, environment={}, authorized=True)
    assert all(b"never-save" not in p.read_bytes() for p in directory.iterdir())


def test_timeout_terminates_spawned_descendant(tmp_path):
    import time
    marker = tmp_path / "child-survived.txt"
    child = "import time; from pathlib import Path; time.sleep(1.5); Path('child-survived.txt').touch()"
    code = ("import subprocess,sys,time; subprocess.Popen([sys.executable, '-I', '-c', "
            + repr(child) + "]); print('spawned', flush=True); time.sleep(20)")
    got = process(tmp_path, code, timeout_seconds=0.4)
    assert got["stop_reason"] == "timeout"
    assert b"spawned" in got["stdout"]
    time.sleep(1.5)
    assert not marker.exists()


def test_successful_transport_is_saved_without_business_acceptance(tmp_path, monkeypatch):
    import json

    from app.analysis_execution import runner
    from app.analysis_execution.runner import execute
    req = request(tmp_path)
    raw = (json.dumps({"type": "system", "subtype": "init", "session_id": req.session_id})
           + "\n" + json.dumps({"type": "result", "subtype": "success", "is_error": False,
           "session_id": req.session_id, "total_cost_usd": 0.1,
           "structured_output": {"synthetic": True}}) + "\n").encode()

    def boundary(argv, **kwargs):
        assert argv[argv.index("--resume") + 1] == req.session_id
        assert kwargs["stdin"] == req.prompt.encode()
        return {"stdout": raw, "stderr": b"", "returncode": 0, "stop_reason": None}

    monkeypatch.setattr(runner, "run_process", boundary)
    root = tmp_path / "records"
    outcome = execute(req, records_root=root, environment={}, authorized=True)
    assert outcome["status"] == "transport_succeeded"
    assert outcome["business_acceptance"] == "not_evaluated"
    assert outcome["estimated_cost_usd"] == "0.1"
    folder = root / req.session_id / req.attempt_id
    saved = json.loads((folder / "outcome.json").read_text())
    assert saved["result"]["structured_output"] == {"synthetic": True}
    assert (folder / "stdout.jsonl").read_bytes() == raw
    assert not list(root.rglob("accepted.json"))
    assert req.prompt.encode() not in (folder / "request.json").read_bytes()


def test_unresolved_attempt_blocks_another_attempt_in_same_session(tmp_path):
    from app.analysis_execution.runner import execute
    req = request(tmp_path)
    root = tmp_path / "records"
    pending = root / req.session_id / str(uuid4())
    pending.mkdir(parents=True)
    (pending / "request.json").write_text('{}')
    with pytest.raises(ValueError, match="session_recovery_required"):
        execute(req, records_root=root, environment={}, authorized=True)
    assert not (root / req.session_id / req.attempt_id).exists()


def test_process_kill_fallback_when_tree_termination_tool_fails(tmp_path, monkeypatch):
    import subprocess

    from app.analysis_execution import process as process_module
    if sys.platform != "win32":
        return  # Windows-specific failure; normal group cleanup tested on every OS.

    def broken_taskkill(*args, **kwargs):
        raise subprocess.TimeoutExpired("taskkill", 5)

    monkeypatch.setattr(process_module.subprocess, "run", broken_taskkill)
    got = process(tmp_path, "import time; print('started', flush=True); time.sleep(20)",
                  timeout_seconds=0.3)
    assert got["returncode"] is not None
    assert got["stop_reason"] == "process_cleanup_unverified"


@pytest.mark.parametrize("environment", [None, [], {"KEY": None}, {1: "value"}])
def test_environment_must_be_explicit_before_any_side_effect(tmp_path, environment):
    from app.analysis_execution.runner import execute
    root = tmp_path / "records"
    with pytest.raises(ValueError, match="invalid_execution_environment"):
        execute(request(tmp_path), records_root=root, environment=environment, authorized=True)
    assert not root.exists()


def test_unverified_cleanup_quarantines_session(tmp_path):
    from app.analysis_execution.runner import execute
    req = request(tmp_path)
    root = tmp_path / "records"
    pending = root / req.session_id / str(uuid4())
    pending.mkdir(parents=True)
    (pending / "outcome.json").write_text('{"error_code":"process_cleanup_unverified"}')
    with pytest.raises(ValueError, match="session_recovery_required"):
        execute(req, records_root=root, environment={}, authorized=True)


def test_resume_command_does_not_offer_agent_creation(tmp_path):
    req = request(tmp_path)
    argv = build_command(req)
    assert 'Agent' not in argv[argv.index('--tools') + 1].split(',')
    assert 'Agent' not in argv[argv.index('--allowedTools') + 1].split(',')
    first = build_command(replace(req, resume=False))
    assert 'Agent' not in first[first.index('--tools') + 1].split(',')


def test_explicit_initialization_definition_enables_only_named_readonly_member(tmp_path):
    from app.analysis_packets.codec import json_bytes
    definition = {'worker-one': {'description': 'analysis worker', 'prompt': 'Use current rules',
                                  'tools': ['Read'], 'model': 'inherit'}}
    req = replace(request(tmp_path), agents=definition, create_agent=True, bare=False)
    argv = build_command(req)
    assert argv[argv.index('--agents') + 1] == json_bytes(definition).decode('utf-8').strip()
    assert 'Agent' in argv[argv.index('--tools') + 1].split(',')
    assert argv[argv.index('--resume') + 1] == req.session_id


@pytest.mark.parametrize('definition', [
    {}, {'bad/name': {}}, {'worker': {'description':'x','prompt':'y','tools':['Bash'],
                                     'model':'inherit'}},
    {'worker': {'description':'x','prompt':'y','tools':['Read'],'model':'other'}},
    {'worker': {'description':'x','prompt':'y','tools':['Read'],'model':'inherit','hooks':{}}},
])
def test_invalid_initializer_definitions_fail_before_launch(tmp_path, definition):
    with pytest.raises(ValueError, match='invalid_agent_definitions'):
        build_command(replace(request(tmp_path), agents=definition))


def test_initializer_definitions_respect_windows_command_limit(tmp_path):
    definition = {'worker': {'description':'x','prompt':'z'*32000,
                             'tools':['Read'],'model':'inherit'}}
    with pytest.raises(ValueError, match='command_too_long'):
        build_command(replace(request(tmp_path), agents=definition))


def test_definitions_can_be_loaded_without_creation_in_managed_resume(tmp_path):
    definition = {'worker': {'description':'x','prompt':'y','tools':['Read'],'model':'inherit'}}
    argv = build_command(replace(request(tmp_path), agents=definition, bare=False))
    assert '--agents' in argv
    assert '--bare' not in argv
    assert 'Agent' not in argv[argv.index('--tools')+1].split(',')
    assert argv[argv.index('--setting-sources')+1] == ''


def test_creation_needs_explicit_definitions(tmp_path):
    with pytest.raises(ValueError, match='agent_definitions_required'):
        build_command(replace(request(tmp_path), create_agent=True))


def test_direct_session_only_allows_read(tmp_path):
    req = replace(request(tmp_path), direct_session=True)
    argv = build_command(req)
    assert argv[argv.index('--tools') + 1] == 'Read'
    assert argv[argv.index('--allowedTools') + 1] == 'Read'
    assert '--agents' not in argv
    assert '--forward-subagent-text' not in argv


@pytest.mark.parametrize('changes', [{'bare': False}, {'create_agent': True}, {'agents': {}}])
def test_direct_session_rejects_native_configuration(tmp_path, changes):
    with pytest.raises(ValueError, match='direct_session_config_invalid'):
        build_command(replace(request(tmp_path), direct_session=True, **changes))
