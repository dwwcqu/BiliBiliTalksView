from test_initialization import (
    initialize,
    install_creation_boundary,
    setup_initialization,
    uncreated_case,
)

from app.analysis_packets.codec import json_bytes, loads


def test_status_preserves_partial_native_ids_without_promoting_them(output_case, monkeypatch):
    from app.analysis_execution import runner
    from app.analysis_execution.initialization_status import initialization_status
    case = uncreated_case(output_case)
    req = setup_initialization(case)
    install_creation_boundary(monkeypatch, req)
    original = runner.run_process

    def stopped(argv, **kwargs):
        result = original(argv, **kwargs)
        events = [loads(line) for line in result['stdout'].decode().split('\n') if line]
        if any(e.get('task_id') == 'native-context' for e in events):
            for event in events:
                if event.get('subtype') == 'task_notification':
                    event['status'] = 'stopped'
                if event.get('type') == 'result':
                    event.update(subtype='error_max_budget_usd', is_error=True)
            result.update(stdout=b''.join(json_bytes(e) for e in events), returncode=1)
        return result

    monkeypatch.setattr(runner, 'run_process', stopped)
    members = [{'member_id':'reader','role':'user_initial'},
               {'member_id':'context','role':'thread_context'},
               {'member_id':'synthesis','role':'user_synthesis'}]
    assert initialize(case, req, members)['status'] == 'blocked'
    status = initialization_status(req.cwd)
    assert status['status'] == 'blocked'
    assert [m['state'] for m in status['members']] == ['verified','created_unverified','not_started']
    assert status['members'][1]['observed_agent_ids'] == ['native-context']
    assert status['members'][1]['reason_code'] == 'native_task_failed'
    assert status['active_estimated_cost_usd'] == '0.02'
    assert not (req.cwd/'sessions/team/team.json').exists()


def test_status_does_not_trust_tampered_transport(output_case, monkeypatch):
    from app.analysis_execution.initialization_status import initialization_status
    case = uncreated_case(output_case)
    req = setup_initialization(case)
    install_creation_boundary(monkeypatch, req)
    initialize(case, req)
    # Committed teams must use the strong complete loader, including all proof hashes.
    (req.cwd/'sessions/team/members/reader/stdout.jsonl').write_bytes(b'{}')
    import pytest
    with pytest.raises(ValueError):
        initialization_status(req.cwd)


def test_partial_status_counts_verified_handshake_repair(output_case, monkeypatch):
    from dataclasses import replace
    from decimal import Decimal
    from uuid import uuid4

    from test_member_repair import pending, repair_boundary, run_repair

    from app.analysis_execution.initialization_status import initialization_status
    _, req, _ = pending(output_case, monkeypatch)
    repair = replace(req, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    repair_boundary(monkeypatch, repair)
    assert run_repair(repair)['status'] == 'repaired'
    status = initialization_status(req.cwd)
    assert [m['state'] for m in status['members']] == ['verified','verified']
    assert status['active_estimated_cost_usd'] == '0.04'
    assert status['initialization_complete'] is False


def test_status_exposes_refusal_and_counts_failed_call_cost(output_case, monkeypatch):
    from dataclasses import replace
    from decimal import Decimal
    from uuid import uuid4

    from test_member_repair import pending, refusal_boundary, run_repair

    from app.analysis_execution.initialization_status import initialization_status
    _, req, _ = pending(output_case, monkeypatch)
    repair = replace(req, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    refusal_boundary(monkeypatch, repair)
    assert run_repair(repair)['error_code'] == 'member_resume_refused'
    status = initialization_status(req.cwd)
    assert status['members'][1]['state'] == 'resume_refused'
    assert status['members'][1]['reason_code'] == 'member_resume_refused'
    assert status['active_estimated_cost_usd'] == '0.04'
    assert status['initialization_complete'] is False



def test_status_marks_unmanaged_call_cost_unknown(output_case, monkeypatch):
    from uuid import uuid4

    from test_member_repair import pending

    from app.analysis_execution.initialization_status import initialization_status
    _, req, _ = pending(output_case, monkeypatch)
    records = req.cwd / 'sessions/cli-calls' / req.session_id
    extra = records / str(uuid4())
    extra.mkdir()
    (extra / 'outcome.json').write_bytes(json_bytes({'estimated_cost_usd': '0.25'}))
    status = initialization_status(req.cwd)
    assert status['active_estimated_cost_usd'] is None
    assert status['status'] == 'blocked'
    assert status['reason_code'] == 'unmanaged_initialization_attempt'


def test_status_marks_orphan_repair_cost_unknown(output_case, monkeypatch):
    from dataclasses import replace
    from decimal import Decimal
    from uuid import uuid4

    from test_member_repair import pending, refusal_boundary, run_repair

    from app.analysis_execution.initialization_status import initialization_status
    _, req, _ = pending(output_case, monkeypatch)
    intent = loads((req.cwd / 'sessions/team/intent.json').read_text(encoding='utf-8'))
    repair = replace(req, attempt_id=str(uuid4()), budget_usd=Decimal('0.05'))
    refusal_boundary(monkeypatch, repair)
    assert run_repair(repair)['error_code'] == 'member_resume_refused'
    original = req.cwd / 'sessions/cli-calls' / req.session_id / intent['steps'][1]['attempt_id']
    original.rename(req.cwd / 'saved-original')
    status = initialization_status(req.cwd)
    assert status['active_estimated_cost_usd'] is None
    assert status['status'] == 'blocked'
    assert status['members'][1]['state'] == 'incomplete_record'
    assert status['members'][1]['reason_code'] == 'orphan_repair_evidence'



def test_status_excludes_valid_archived_recovery_call(output_case, monkeypatch):
    from dataclasses import replace
    from uuid import uuid4

    from test_initialization import empty_first

    from app.analysis_execution import runner
    from app.analysis_execution.initialization_status import initialization_status
    case = uncreated_case(output_case)
    req = setup_initialization(case)
    empty_first(case, req, monkeypatch)
    recovered = replace(req, attempt_id=str(uuid4()))
    install_creation_boundary(monkeypatch, req)
    boundary = runner.run_process

    def wrong_handshake(argv, **kwargs):
        result = boundary(argv, **kwargs)
        events = [loads(line) for line in result['stdout'].decode().splitlines() if line]
        for event in events:
            if event.get('subtype') == 'task_notification':
                event['summary'] = '{}'
        return result | {'stdout': b''.join(json_bytes(e) for e in events)}

    monkeypatch.setattr(runner, 'run_process', wrong_handshake)
    assert initialize(case, recovered, recover_empty=True)['status'] == 'blocked'
    status = initialization_status(req.cwd)
    assert status['active_estimated_cost_usd'] == '0.01'
    assert status['reason_code'] is None
