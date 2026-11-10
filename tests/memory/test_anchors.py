"""PostgreSQL bookkeeping and deterministic fake generation for rolling anchors."""
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import SessionInfo
from evowiki.memory.anchors import BATCH_TURNS, AnchorMemory, covered_turns
from evowiki.memory.contracts import EvidenceRef
from evowiki.memory.store import Item, PgMemoryStore
from evowiki.store import dreaming
from tests.runtime.helpers import make_user

T = datetime(2026, 11, 8, tzinfo=UTC)


class Gateway:
    metered = False

    def __init__(self):
        self.requests, self.callback, self.answer, self.fail = [], None, None, False

    def call(self, role, request, schema, *, account):
        self.requests.append(request)
        if self.callback:
            callback, self.callback = self.callback, None
            callback()
        if self.fail:
            raise RuntimeError('fake generation failure')
        data = json.loads(request.prompt)
        previous = data['previous']
        new = [{'text': raw['question'], 'sources': [node]} for node, raw in data['raw'].items()
               if not any(node in s['sources'] for s in previous)]
        values = self.answer(data) if self.answer else (previous + new)[-24:]
        return schema.model_validate({'statements': values}), SimpleNamespace(id=f'fake{len(self.requests)}')


@pytest.fixture
def w(pool):
    make_user(pool, 'u')
    sessions, memory = PgSessionStore(pool), PgMemoryStore(pool)
    state = SimpleNamespace(pages={'p1': object(), 'p2': object()}, paths={'g1': object(), 'g2': object()}, maps={})
    repo = SimpleNamespace(load=lambda uid: SimpleNamespace(state=state))
    return SimpleNamespace(pool=pool, sessions=sessions, memory=memory, state=state,
                           anchors=AnchorMemory(pool, sessions, memory, repo=repo), gateway=Gateway())


def session(w, sid='s1', page='p1'):
    info = SessionInfo(session_id=sid, user_id='u', page_id=page, note_concept_id=None, created_at=T)
    w.sessions.create(info)
    return info


def turn(w, sid, i, text=None, *, tool=None):
    at = T + timedelta(seconds=i)
    w.sessions.append(sid, f'{sid}-u{i}', 'user', {'message_id': f'm{i}', 'text': text or f'question {i}'}, at)
    if tool:
        w.sessions.append(sid, f'{sid}-t{i}', 'tool', {'message_id': f'm{i}', 'name': 'set_goal_deadline',
                          'call_id': f'call{i}', 'ok': True, 'effect': 'propose', 'args': tool, 'content': 'accepted'}, at)
    w.sessions.append(sid, f'{sid}-a{i}', 'assistant', {'message_id': f'a{i}', 'text': f'answer {i}', 'sources': []}, at)


def update(w, info):
    return w.anchors.update(info, gateway=w.gateway, account=None, now=T + timedelta(days=1))


def raw(w, key='page:p1'):
    with w.pool.connection() as conn:
        return dreaming.get(conn, 'u', 'anchor', key)


def test_update_without_new_evidence_costs_zero_calls(w):
    info = session(w)
    turn(w, 's1', 1)
    assert update(w, info)
    row = w.anchors.readable('u', 'page:p1')
    assert row.watermarks == {'s1': 1} and covered_turns(row, 's1') == 1
    assert {'session:s1', 'turn:s1/m1'} <= set(row.sources)
    assert update(w, info) and len(w.gateway.requests) == 1
    assert w.gateway.requests[0].purpose == 'memory_anchor'
    w.sessions.append('s1', 'compact', 'compaction', {'summary': 'not raw evidence'}, T)
    assert update(w, info) and len(w.gateway.requests) == 1


def test_batches_resume_after_failure_and_preserve_other_sessions(w):
    a, b = session(w, 'a'), session(w, 'b')
    for i in range(1, 86):
        turn(w, 'a', i)
    turn(w, 'b', 1, 'another session')
    assert not update(w, a)
    first = raw(w)
    assert first.watermarks['a'] == BATCH_TURNS
    w.gateway.fail = True
    with pytest.raises(RuntimeError, match='fake generation failure'):
        update(w, a)
    assert raw(w).version == first.version
    w.gateway.fail = False
    for _ in range(10):
        if update(w, b):
            break
    else:
        pytest.fail('bounded batches did not converge')
    row = raw(w)
    assert row.watermarks == {'a': 85, 'b': 1}
    assert {'session:a', 'session:b'} <= set(row.sources)
    assert all(len(json.loads(r.prompt)['raw']) <= BATCH_TURNS for r in w.gateway.requests)
    assert update(w, a)


def test_changes_during_generation_do_not_publish_stale_context(w):
    info = session(w)
    turn(w, 's1', 1)
    w.gateway.callback = lambda: turn(w, 's1', 2, 'late evidence')
    assert not update(w, info)
    assert raw(w) is None
    assert update(w, info) and raw(w).watermarks == {'s1': 2}


def test_publication_cas_preserves_concurrent_anchor(w):
    info = session(w)
    turn(w, 's1', 1)

    def competitor():
        with w.pool.connection() as conn:
            assert dreaming.publish(conn, 'u', 'anchor', 'page:p1', expected_version=0,
                                    payload={'statements': []}, watermarks={}, sources=['session:s1'],
                                    fingerprint='concurrent', now=T)
    w.gateway.callback = competitor
    assert not update(w, info)
    assert raw(w).fingerprint == 'concurrent'
    assert update(w, info)


def test_goal_scope_is_per_turn_and_pageless_items_need_real_evidence(w):
    info = session(w)
    turn(w, 's1', 1, 'plain page question')
    turn(w, 's1', 2, 'continue goal g1')
    turn(w, 's1', 3, 'schedule it', tool={'goal_id': 'g2'})
    turn(w, 's1', 4, 'compare g1 and g2')
    assert update(w, info)
    goal1, goal2, page = raw(w, 'goal:g1'), raw(w, 'goal:g2'), raw(w)
    assert 'turn:s1/m2' in goal1.sources and 'turn:s1/m1' not in goal1.sources
    assert 'turn:s1/m3' in goal2.sources and 'turn:s1/m2' not in goal2.sources
    assert {'turn:s1/m1', 'turn:s1/m4'} <= set(page.sources)
    assert 'turn:s1/m2' not in page.sources and 'turn:s1/m3' not in page.sources
    loose = session(w, 'loose', None)
    turn(w, 'loose', 1, 'specific decision discussion')
    w.memory.put_item(Item(item_id='d1', user_id='u', kind='decision', content='decided scope',
        provenance='explicit_history', evidence=(EvidenceRef(session_id='loose', message_id='m1', role='user'),),
        scope_type='global', session_id='loose', valid_from=T, created_at=T, status='active'))
    assert update(w, loose) and raw(w, 'memory_item:d1') is not None
    del w.state.paths['g1']
    assert w.anchors.readable('u', 'goal:g1') is None


def test_learning_needs_a_real_self_report_and_survives_prose_limit(w):
    info = session(w)
    turn(w, 's1', 1, '为什么这样？')
    turn(w, 's1', 2, '标准差我还没懂')
    w.gateway.answer = lambda data: [
        {'text': 'not valid', 'sources': ['turn:s1/m1'], 'state': 'unclear', 'concept': 'question', 'evidence_quote': '为什么这样？'},
        {'text': '标准差还没懂', 'sources': ['turn:s1/m2'], 'state': 'unclear', 'concept': '标准差', 'evidence_quote': '标准差我还没懂'}]
    assert update(w, info)
    assert [v['concept'] for v in raw(w).payload['learning']] == ['标准差']
    w.gateway.answer = None
    for i in range(3, 55):
        turn(w, 's1', i)
    for _ in range(10):
        if update(w, info):
            break
    else:
        pytest.fail('batch did not converge')
    assert raw(w).payload['learning'][0]['concept'] == '标准差'
    assert len(raw(w).payload['statements']) <= 24


def test_feedback_correction_rebuilds_linked_sessions_from_originals(w):
    a, b = session(w, 'a'), session(w, 'b')
    turn(w, 'a', 1, 'older evidence')
    turn(w, 'b', 1, 'newer evidence')
    assert update(w, a) and update(w, b)
    w.sessions.append('a', 'rate', 'rating', {'message_id': 'a1', 'turn_id': 'm1',
                      'value': 'down', 'note': 'correction'}, T + timedelta(hours=1))
    assert update(w, a)
    data = json.loads(w.gateway.requests[-1].prompt)
    assert set(data['raw']) == {'turn:a/m1', 'turn:b/m1'} and data['previous'] == []
    assert any(v['note'] == 'correction' for v in data['feedback'].values())


def test_current_suppression_vetoes_publish_and_does_not_restore_raw(w):
    info = session(w)
    turn(w, 's1', 1, 'private evidence')

    def suppress():
        with w.pool.connection() as conn:
            conn.execute('insert into memory_suppression '
                         '(user_id,item_id,kind,evidence_message_ids,scope_type,at) '
                         'values(%s,%s,%s,%s,%s,%s)', ('u', 'suppressed', 'episode', ['m1'], 'global', T))
    w.gateway.callback = suppress
    assert not update(w, info)
    assert raw(w) is None
    calls = len(w.gateway.requests)
    assert update(w, info)
    assert len(w.gateway.requests) == calls


def test_explicit_later_understanding_resolves_only_the_matching_learning_scope(w):
    info = session(w)
    turn(w, 's1', 1, '标准差我还没懂')
    w.gateway.answer = lambda data: [{'text': '标准差还没懂', 'sources': ['turn:s1/m1'],
        'state': 'unclear', 'concept': '标准差', 'evidence_quote': '标准差我还没懂'}]
    assert update(w, info)
    turn(w, 's1', 2, '标准差我现在明白了')
    w.gateway.answer = lambda data: [{'text': '标准差已明白', 'sources': ['turn:s1/m2'],
        'state': 'understood', 'concept': '标准差', 'evidence_quote': '标准差我现在明白了'}]
    assert update(w, info)
    assert [v['state'] for v in raw(w).payload['learning']] == ['understood']
    turn(w, 's1', 3, '标准差为什么这样算？')
    w.gateway.answer = lambda data: [{'text': 'invented confusion', 'sources': ['turn:s1/m3'],
        'state': 'unclear', 'concept': '标准差', 'evidence_quote': '标准差为什么这样算？'}]
    assert update(w, info)
    assert [v['state'] for v in raw(w).payload['learning']] == ['understood']


def test_published_turn_scope_is_not_reassigned_after_a_goal_is_renamed(w):
    info = session(w)
    w.state.paths['g1'] = SimpleNamespace(path=SimpleNamespace(goal='Algebra'))
    turn(w, 's1', 1, '继续学习 Algebra 目标')
    assert update(w, info)
    assert raw(w, 'goal:g1') is not None
    w.state.paths['g1'].path.goal = 'New Algebra'
    turn(w, 's1', 2, 'plain page question')
    assert update(w, info)
    assert 'turn:s1/m1' not in raw(w).sources
    assert 'turn:s1/m1' in raw(w, 'goal:g1').sources


def test_learning_collection_is_independent_of_the_24_statement_overview(w):
    info = session(w)
    for i in range(1, 41):
        turn(w, 's1', i, f'概念{i}我还没懂')

    class LearningGateway(Gateway):
        def call(self, role, request, schema, *, account):
            self.requests.append(request)
            data = json.loads(request.prompt)
            values = [{'text': item['question'], 'sources': [node], 'state': 'unclear',
                       'concept': item['question'].split('我')[0],
                       'evidence_quote': item['question']} for node, item in data['raw'].items()]
            return schema.model_validate({'learning': values}), SimpleNamespace(id='learning-batch')
    w.gateway = LearningGateway()
    assert update(w, info)
    assert len(raw(w).payload['learning']) == 40 and raw(w).payload['statements'] == []


def test_plain_goal_browsing_and_failed_operations_do_not_assign_scope(w):
    info = session(w)
    for i, ok in enumerate((True, False), 1):
        at = T + timedelta(seconds=i)
        w.sessions.append('s1', f'u{i}', 'user', {'message_id': f'm{i}', 'text': 'show options'}, at)
        w.sessions.append('s1', f't{i}', 'tool', {'message_id': f'm{i}', 'call_id': f'c{i}',
                          'name': 'read_goals' if ok else 'set_goal_deadline',
                          'effect': 'read' if ok else 'propose', 'args': {'goal_id': 'g1'},
                          'content': 'goals', 'ok': ok}, at)
        w.sessions.append('s1', f'a{i}', 'assistant', {'message_id': f'a{i}', 'text': 'answer'}, at)
    assert update(w, info)
    assert raw(w, 'goal:g1') is None and raw(w).watermarks == {'s1': 2}


def test_same_page_continue_keeps_cumulative_explicit_goal_beyond_three_raw_turns(w):
    from evowiki.assistant.carryover import carry_over

    prior = session(w, 'prior')
    for index in range(1, 11):
        turn(w, 'prior', index, f'goal g1 milestone {index}')
    assert update(w, prior)
    fresh = session(w, 'fresh')
    carried = carry_over(w.sessions, fresh, anchors=w.anchors, question='继续')
    assert '[goal:g1]' in carried.page_summary and 'milestone 1' in carried.page_summary
    assert not carried.page_recent
    assert [row['id'] for row in carried.recalled] == [w.anchors.readable('u', 'goal:g1').node]


def test_goal_continuity_uses_original_page_evidence_and_current_explicit_request(w):
    a = session(w, 'a', page='p1')
    turn(w, 'a', 1, 'goal g1 page one evidence')
    assert update(w, a)
    b = session(w, 'b', page='p2')
    turn(w, 'b', 1, 'goal g2 other page evidence')
    assert update(w, b)
    assert [row.key for row in w.anchors.continuity('u', 'p1', '继续')] == ['goal:g1']
    assert [row.key for row in w.anchors.continuity('u', 'p1', '继续 g2')] == ['goal:g2']
    # A second real goal discussion on p1 leaves both labelled; no active goal is guessed.
    c = session(w, 'c', page='p1')
    turn(w, 'c', 1, 'goal g2 explicit discussion here')
    assert update(w, c)
    assert {row.key for row in w.anchors.continuity('u', 'p1', '继续')} == {'goal:g1', 'goal:g2'}
    w.state.paths['g1'] = SimpleNamespace(path=SimpleNamespace(goal='学统计'))
    w.state.paths['g2'] = SimpleNamespace(path=SimpleNamespace(goal='学数学'))
    assert [row.key for row in w.anchors.continuity('u', 'p1', '继续学数学')] == ['goal:g2']
    w.state.paths['g2'].path.goal = '学统计'
    assert {row.key for row in w.anchors.continuity('u', 'p1', '继续目标学统计')} == {'goal:g1', 'goal:g2'}


def test_goal_context_budget_records_only_rows_actually_injected(w):
    from evowiki.assistant.carryover import CARRY_BUDGET, carry_over, render_carry
    from evowiki.goals.context import estimate_tokens
    from evowiki.llm.chat import memory_fenced

    session(w, 'old')
    turn(w, 'old', 1, 'ordinary page evidence')
    for index in range(12):
        key = f'goal:long{index}'
        w.state.paths[f'long{index}'] = object()
        with w.pool.connection() as conn:
            assert dreaming.publish(conn, 'u', 'anchor', key, expected_version=0,
                       payload={'statements': [{'text': str(index) + '长' * 590,
                                               'sources': ['turn:old/m1']}]},
                       watermarks={'old': 1}, sources=['turn:old/m1'], fingerprint=key, now=T)
    fresh = session(w, 'fresh')
    carried = carry_over(w.sessions, fresh, anchors=w.anchors, question='继续')
    assert estimate_tokens(memory_fenced(render_carry(carried))) <= CARRY_BUDGET
    assert 0 < len(carried.recalled) < 12
    assert all(row['id'].rsplit(':', 1)[0].removeprefix('dream:anchor:') in carried.page_summary
               for row in carried.recalled)
