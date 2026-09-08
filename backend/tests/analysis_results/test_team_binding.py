from pathlib import Path

import pytest


def test_binding_requires_proven_team(output_case):
    from app.analysis_execution.team_binding import bind_team
    with pytest.raises(ValueError, match='team_missing'):
        bind_team(output_case['root'], output_case['assembly'].run_id,
                  output_case['publication']['manifest_id'], output_case['rules'])


def test_new_manifest_uses_real_team_ids_without_rewriting_old(output_case, monkeypatch):
    from test_initialization import (
        initialize,
        install_creation_boundary,
        setup_initialization,
        uncreated_case,
    )

    from app.analysis_execution.team_binding import bind_team
    from app.analysis_results.acceptance import read_state
    case = uncreated_case(output_case)
    req = setup_initialization(case)
    calls = install_creation_boundary(monkeypatch, req)
    team = initialize(case, req)
    assert team['status'] == 'initialized'
    old = case['publication']
    old_raw = Path(old['manifest_path']).read_bytes()
    bound = bind_team(case['root'], case['assembly'].run_id, old['manifest_id'], case['rules'])
    assert bound['manifest_id'] != old['manifest_id']
    assert Path(old['manifest_path']).read_bytes() == old_raw
    _, _, assembly, _ = read_state(case['root'], case['assembly'].run_id,
                                   bound['manifest_id'], case['rules'])
    assert assembly.member_registry['main']['session_id'] == req.session_id
    assert {m['agent_id'] for m in assembly.member_registry['members']} == {
        'native-reader', 'native-context'}
    tasks = [tid for m in assembly.member_registry['members'] for tid in m['task_ids']]
    assert sorted(tasks) == sorted(p['task_id'] for p in assembly.packets)
    repeated = bind_team(case['root'], case['assembly'].run_id, old['manifest_id'], case['rules'])
    assert repeated['manifest_id'] == bound['manifest_id']
    assert len(calls) == 2


def test_team_environment_rejects_mismatched_session_and_state(output_case, monkeypatch):
    from test_initialization import (
        initialize,
        install_creation_boundary,
        setup_initialization,
        uncreated_case,
    )

    from app.analysis_execution.team_binding import team_environment
    case = uncreated_case(output_case)
    req = setup_initialization(case)
    install_creation_boundary(monkeypatch, req)
    team = initialize(case, req)
    assert team['status'] == 'initialized'
    source = {'SAFE_SETTING': 'value'}
    result = team_environment(req.cwd, req.session_id, team['members'], source)
    assert result['CLAUDE_CONFIG_DIR'] == str(req.cwd / 'sessions/claude-state')
    assert 'CLAUDE_CONFIG_DIR' not in source
    with pytest.raises(ValueError, match='team_registry_conflict'):
        team_environment(req.cwd, 'wrong-session', team['members'], {})
    with pytest.raises(ValueError, match='cli_state_dir_conflict'):
        team_environment(req.cwd, req.session_id, team['members'], {'CLAUDE_CONFIG_DIR':'wrong'})


def test_analysis_dispatch_cannot_request_new_members(output_case):
    from dataclasses import replace

    from test_dispatch import dispatch, setup_request
    req, config = setup_request(output_case)
    with pytest.raises(ValueError, match='initialization_not_allowed_in_dispatch'):
        dispatch(output_case, replace(req, agents={}), config, authorized=True)


def test_registered_role_definition_is_reloaded_without_creation(output_case, monkeypatch):
    from test_initialization import (
        initialize,
        install_creation_boundary,
        setup_initialization,
        uncreated_case,
    )

    from app.analysis_execution.team_binding import team_definitions
    case = uncreated_case(output_case)
    req = setup_initialization(case)
    install_creation_boundary(monkeypatch, req)
    assert initialize(case, req)['status'] == 'initialized'
    definition = team_definitions(req.cwd, 'reader')
    assert list(definition) == ['reader']
    assert definition['reader']['tools'] == ['Read']
    assert definition['reader']['model'] == 'inherit'
    with pytest.raises(ValueError, match='team_member_missing'):
        team_definitions(req.cwd, 'not-registered')


def test_initialized_bound_team_dispatch_uses_same_history_and_definition(output_case, monkeypatch):
    from test_dispatch import native_stream, setup_request
    from test_initialization import (
        initialize,
        install_creation_boundary,
        setup_initialization,
        uncreated_case,
    )

    from app.analysis_execution import runner
    from app.analysis_execution.delivery import prepare_delivery
    from app.analysis_execution.dispatch import dispatch_task
    from app.analysis_execution.team_binding import bind_team
    from app.analysis_results.acceptance import read_state

    case = uncreated_case(output_case)
    initial = setup_initialization(case)
    install_creation_boundary(monkeypatch, initial)
    assert initialize(case, initial)['status'] == 'initialized'
    case['publication'] = bind_team(case['root'], case['assembly'].run_id,
                                    case['publication']['manifest_id'], case['rules'])
    _, _, case['assembly'], _ = read_state(case['root'], case['assembly'].run_id,
                                          case['publication']['manifest_id'], case['rules'])
    req, config = setup_request(case)
    packet = case['assembly'].packets[0]
    member = next(m for m in case['assembly'].member_registry['members']
                  if packet['task_id'] in m['task_ids'])
    delivery = prepare_delivery(case['root'], case['assembly'].run_id,
                                case['publication']['manifest_id'], packet['task_id'],
                                req.attempt_id, member['agent_id'], case['rules'])

    def boundary(argv, **kwargs):
        assert '--bare' not in argv
        assert '--agents' in argv
        assert 'Agent' not in argv[argv.index('--tools')+1].split(',')
        assert kwargs['env']['CLAUDE_CONFIG_DIR'] == str(req.cwd / 'sessions/claude-state')
        assert kwargs['stdin'] == delivery.prompt.encode('utf-8')
        return {'stdout':native_stream(delivery,config,req), 'stderr':b'',
                'returncode':0,'stop_reason':None}

    monkeypatch.setattr(runner,'run_process',boundary)
    result = dispatch_task(case['root'],case['assembly'].run_id,case['publication']['manifest_id'],
                           packet['task_id'],request=req,agent_id=member['agent_id'],
                           rule_catalog=case['rules'],execution_config=config,
                           environment={},authorized=True)
    assert result['status'] == 'accepted'
