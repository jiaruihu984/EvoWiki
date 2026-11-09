"""All proposer reads must obey the same evaluation boundary and source closure."""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

from evowiki.agent.tools import ToolResult
from evowiki.harness.improver import ImproverRun, _ToolError
from evowiki.harness.snapshot import SnapshotBoundary
from evowiki.store import dreaming, harness_rsi
from evowiki.store.trace import PgTraceSink
from tests.harness.test_improver import CASES, NOW, Improver, deps
from tests.runtime.helpers import make_user


def database_now(pool):
    with pool.connection() as conn:
        return conn.execute('select clock_timestamp() as at').fetchone()['at']


def freeze(state, pool):
    state.deps = replace(state.deps, snapshot=SnapshotBoundary(
        database_now(pool), state.deps.snapshot.excluded_sources))


@pytest.fixture
def state(pool):
    make_user(pool, 'u1')
    return ImproverRun('u1', CASES, replace(deps(pool, Improver()),
                       snapshot=SnapshotBoundary(database_now(pool), frozenset({'turn:s/held'}))))


def traced(pool, run_id, at=NOW, *, sources=()):
    sink = PgTraceSink(pool, lambda: at)
    sink.start(run_id, user_id='u1', agent='assistant.turn', subject='s', pack_version=0,
               input={'sources': list(sources)})
    sink.tool(run_id, 1, 'call1', 'read_page', {}, ToolResult(content='PRIVATE ANSWER'))
    sink.finish(run_id, stop='finished', error=None)


@pytest.mark.parametrize(('run_id', 'at', 'sources'), [
    ('assistant:s:future', NOW + timedelta(days=1), ()),
    ('assistant:s:held', NOW - timedelta(days=1), ()),
    ('assistant:s:derived', NOW - timedelta(days=1), ('turn:s/held',)),
])
def test_read_trace_rejects_future_heldout_and_explicit_derived_sources(pool, state, run_id, at, sources):
    traced(pool, run_id, at, sources=sources)
    if run_id != 'assistant:s:future':
        freeze(state, pool)
    with pytest.raises(_ToolError):
        state.read_trace({'run_id': run_id, 'seq': 1})
    assert f'trace:{run_id}' not in state.known()


def test_read_trace_checks_current_suppression(pool, state):
    traced(pool, 'assistant:s:withdrawn', NOW - timedelta(days=1))
    with pool.connection() as conn:
        conn.execute('insert into memory_suppression '
                     '(user_id,item_id,kind,evidence_message_ids,scope_type,at) '
                     'values(%s,%s,%s,%s,%s,%s)',
                     ('u1', 'suppressed', 'episode', ['withdrawn'], 'global', NOW))
    freeze(state, pool)
    with pytest.raises(_ToolError):
        state.read_trace({'run_id': 'assistant:s:withdrawn', 'seq': 1})


def test_read_trace_expands_transitive_generation_dependencies(pool, state):
    run_id = 'assistant:s:allowed'
    traced(pool, run_id, NOW - timedelta(days=1))
    with pool.connection() as conn:
        dreaming.register_consumption(conn, 'u1', 'round:historic', ['turn:s/held'])
        dreaming.register_consumption(conn, 'u1', 'trace:' + run_id, ['round:historic'])
    freeze(state, pool)
    with pytest.raises(_ToolError):
        state.read_trace({'run_id': run_id, 'seq': 1})


def test_read_experience_expands_indirect_heldout_dependencies(pool, state):
    with pool.connection() as conn:
        identity = harness_rsi.add_experience(conn, 'u1', {'problem': 'secret lesson'}, {}, {},
                    'effective', 'user', weight=1, now=NOW - timedelta(days=1))
        dreaming.register_consumption(conn, 'u1', 'round:historic', ['turn:s/held'])
        dreaming.register_consumption(conn, 'u1', 'experience:' + identity, ['round:historic'])
    freeze(state, pool)
    page = json.loads(state.read_experience({'query': 'secret lesson'}))
    assert not page['items']
    with pytest.raises(_ToolError):
        state._cited(['experience:' + identity])


def test_allowed_historical_trace_remains_readable(pool, state):
    traced(pool, 'assistant:s:train', NOW - timedelta(days=1))
    freeze(state, pool)
    assert 'PRIVATE ANSWER' in state.read_trace({'run_id': 'assistant:s:train', 'seq': 1})
    assert state._cited(['trace:assistant:s:train']) == ('trace:assistant:s:train',)


def test_signal_text_and_citation_ids_obey_cutoff_and_heldout_sources(pool, state):
    from evowiki.teach.contracts import Signal

    signals = (
        Signal(signal_id='future', kind='page_feedback', at=state.deps.snapshot.cutoff + timedelta(days=1), text='FUTURE FEEDBACK'),
        Signal(signal_id='held', kind='assistant_rejected', at=NOW, text='HELDOUT FEEDBACK'),
        Signal(signal_id='train', kind='page_feedback', at=NOW, text='TRAIN FEEDBACK'),
    )
    state = ImproverRun('u1', CASES, replace(state.deps, signals=signals,
                        signal_runs={'held': 'assistant:s:held'}))
    text = state.read_signals({})
    assert 'TRAIN FEEDBACK' in text
    assert 'FUTURE FEEDBACK' not in text and 'HELDOUT FEEDBACK' not in text
    assert 'signal:future' not in state.known() and 'signal:held' not in state.known()


def test_read_pack_uses_the_frozen_round_pack_even_when_current_pack_changes(pool, state):
    from evowiki.harness.pack import HarnessPack
    from evowiki.store import harness

    frozen = HarnessPack(1, {'H1.assistant': 'FROZEN GUIDANCE'})
    state = ImproverRun('u1', CASES, replace(state.deps, base_pack=frozen))
    with pool.connection() as conn:
        harness.set_value(conn, 'u1', 'H1.assistant', 'FUTURE GUIDANCE', source='user', why='later',
                          now=NOW + timedelta(days=1))
    text = state.read_pack({'dim': 'H1'})
    assert 'FROZEN GUIDANCE' in text and 'FUTURE GUIDANCE' not in text


def test_recording_clock_excludes_a_round_finished_late_with_an_old_logical_date(pool, state):
    from evowiki.harness.experience import load_dossiers
    from evowiki.harness.snapshot import recorded_at

    with pool.connection() as conn:
        identity = harness_rsi.start_round(conn, 'u1', finding='late result', now=NOW)
        before = recorded_at(conn, 'u1', 'harness_round', identity)
    cutoff = database_now(pool)
    with pool.connection() as conn:
        harness_rsi.finish_round(conn, identity, 'no_change', reason='observed after cutoff', now=NOW)
        after = recorded_at(conn, 'u1', 'harness_round', identity)
        assert before <= cutoff < after
        assert not load_dossiers(conn, 'u1', cutoff=cutoff)
        assert harness_rsi.rounds(conn, 'u1', 1)[0].round_id == identity
    with pool.connection() as conn:
        assert any(d['id'] == 'round:' + identity for d in load_dossiers(
            conn, 'u1', cutoff=database_now(pool)))


def test_same_session_only_actual_preceding_heldout_history_blocks_trace_and_signal(pool, state):
    from evowiki.assistant.pg_store import PgSessionStore
    from evowiki.assistant.session import SessionInfo
    from evowiki.teach.contracts import Signal

    sessions = PgSessionStore(pool)
    sessions.create(SessionInfo(session_id='history', user_id='u1', page_id=None,
                               note_concept_id=None, created_at=NOW))
    for index in range(1, 4):
        sessions.append('history', f'h-user{index}', 'user',
                        {'message_id': f'm{index}', 'text': f'question {index}'}, NOW)
        sessions.append('history', f'h-answer{index}', 'assistant',
                        {'message_id': f'a{index}', 'text': f'answer {index}', 'sources': []}, NOW)
        traced(pool, f'assistant:history:m{index}', NOW)
    boundary = SnapshotBoundary(database_now(pool), frozenset({'turn:history/m2'}))
    signals = (Signal(signal_id='earlier', kind='assistant_rejected', at=NOW, text='EARLIER FEEDBACK'),
               Signal(signal_id='later', kind='assistant_rejected', at=NOW, text='LATER FEEDBACK'))
    current = ImproverRun('u1', CASES, replace(state.deps, snapshot=boundary, signals=signals,
                          signal_runs={'earlier': 'assistant:history:m1', 'later': 'assistant:history:m3'}))
    assert 'PRIVATE ANSWER' in current.read_trace({'run_id': 'assistant:history:m1', 'seq': 1})
    with pytest.raises(_ToolError):
        current.read_trace({'run_id': 'assistant:history:m3', 'seq': 1})
    text = current.read_signals({})
    assert 'EARLIER FEEDBACK' in text and 'LATER FEEDBACK' not in text
    assert 'signal:earlier' in current.known() and 'signal:later' not in current.known()
