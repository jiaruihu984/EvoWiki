from datetime import timedelta
from types import SimpleNamespace

from evowiki.agent.tools import ToolContext
from evowiki.assistant.carryover import CARRY_BUDGET, carry_over, render_carry
from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import MemorySessionStore, SessionInfo, fold
from evowiki.assistant.tools.memory import ReadPastTurnTool
from evowiki.goals.context import estimate_tokens
from evowiki.llm.chat import memory_fenced
from evowiki.memory.ports import MemoryPorts
from evowiki.memory.recall import RECALL_BUDGET, recall_block
from evowiki.memory.store import InMemoryMemoryStore
from evowiki.store import dreaming
from tests.assistant.fakes import T0


def info(sid):
    return SessionInfo(session_id=sid, user_id='u1', page_id='p1', note_concept_id=None, created_at=T0)


def add_turn(sessions, sid, mid, question, answer):
    sessions.append(sid, sid + mid + '-u', 'user', {'message_id': mid, 'text': question, 'selection': None}, T0)
    sessions.append(sid, sid + mid + '-a', 'assistant', {'message_id': mid + '-a', 'text': answer,
                    'sources': [], 'proposals': [], 'tool_calls': 0, 'llm_calls': 0}, T0)


def test_carry_and_recall_exclude_duplicate_and_current_turns():
    sessions, memory = MemorySessionStore(), InMemoryMemoryStore()
    for sid in ('old', 'current'):
        sessions.create(info(sid))
        add_turn(sessions, sid, 'm', 'atomic question', 'answer')
    statements = [{'text': 'atomic page context', 'sources': ['turn:old/m']},
                  {'text': 'live duplicate', 'sources': ['turn:current/m']}]
    page = dreaming.Summary('u1', 'anchor', 'page:p1', 1, {'statements': statements}, {}, 'f', False,
                            T0, ('turn:old/m', 'turn:current/m'))
    goal = dreaming.Summary('u1', 'anchor', 'goal:g', 1, {'statements': [*statements,
                            {'text': 'atomic independent detail', 'sources': ['turn:other/m']}]},
                            {}, 'g', False, T0, (*page.sources, 'turn:other/m'))
    anchors = SimpleNamespace(continuity=lambda *a: [page], search=lambda *a, **kw: [goal])
    carried = carry_over(sessions, info('current'), anchors=anchors)
    assert not carried.page_recent and 'live duplicate' not in carried.page_summary
    assert estimate_tokens(memory_fenced(render_carry(carried))) <= CARRY_BUDGET
    recalled = recall_block(MemoryPorts(memory, None, sessions, anchors=anchors), info=info('current'),
                            query='atomic', page_id='p1', concept_ids=(),
                            state=fold(info('current'), sessions.entries('current')),
                            known_concepts=frozenset(), now=T0,
                            excluded_sources=frozenset(carried.recalled[0]['covered_sources']))
    assert 'independent detail' in recalled.message.text
    assert 'page context' not in recalled.message.text and 'live duplicate' not in recalled.message.text
    assert estimate_tokens(memory_fenced(recalled.message.text)) <= RECALL_BUDGET


def test_source_preview_and_full_text_paging():
    sessions, memory = MemorySessionStore(), InMemoryMemoryStore()
    sessions.create(info('old'))
    add_turn(sessions, 'old', 'm', 'question', 'long answer ' * 2000 + 'FINAL_SENTINEL')
    anchors = SimpleNamespace(expand=lambda *a: {'sources': ['turn:old/m'], 'has_more': False, 'next_cursor': None})
    tool = ReadPastTurnTool(MemoryPorts(memory, None, sessions, anchors=anchors))
    ctx = ToolContext(user_id='u1', session_id='current')
    preview = tool.run({'anchor_id': 'page:p1', 'version': 1}, ctx)
    assert preview.data['preview_only'] and 'FINAL_SENTINEL' not in preview.content
    cursor, collected = 0, []
    while True:
        page = tool.run({'session_id': 'old', 'message_id': 'm', 'cursor': cursor}, ctx)
        collected.append(page.content)
        assert len(page.content) <= 6000
        if not page.data['has_more']:
            break
        cursor = page.data['next_cursor']
    assert 'FINAL_SENTINEL' in ''.join(collected)


def test_bot_goal_learning_and_new_compaction_after_partial_coverage(pool):
    from evowiki.bot.facts import _open_questions
    from tests.runtime.helpers import make_user

    make_user(pool, 'u1')
    sessions = PgSessionStore(pool)
    sessions.create(info('old'))
    add_turn(sessions, 'old', 'm1', 'old confusion', 'a')
    add_turn(sessions, 'old', 'm2', 'new confusion', 'b')
    sessions.append('old', 'compact', 'compaction', {'version': 'compact@v7', 'handoff': {},
                    'learning': {'unclear': [{'concept': 'new point', 'evidence_id': 'm2'}],
                                 'understood': [{'concept': 'old point', 'evidence_id': 'm2'}]},
                    'first_kept': None}, T0 + timedelta(hours=1))
    with pool.connection() as conn:
        assert dreaming.publish(conn, 'u1', 'anchor', 'goal:g', expected_version=0,
                    payload={'statements': [{'text': 'prose must not become learning'}],
                             'learning': [{'state': 'unclear', 'text': 'old point', 'concept': 'old point',
                                           'sources': ['turn:old/m1'], 'recorded_at': T0.isoformat()}]},
                    watermarks={'old': 1}, sources=['turn:old/m1'], fingerprint='f', now=T0)
    with pool.connection() as conn:
        assert [row.text for row in _open_questions(conn, 'u1', {})] == ['new point']


def test_generation_feedback_and_hard_forget_pipeline(pool, tmp_path):
    import json

    from evowiki.memory.anchors import AnchorMemory, AnchorReply, Statement
    from tests.memory.forget_world import build

    world = build(pool, tmp_path)
    anchors = AnchorMemory(pool, world.sessions, world.mem)

    class Gateway:
        metered = False

        def __init__(self):
            self.calls = []

        def call(self, role, request, schema, *, account):
            self.calls.append(request)
            raw = json.loads(request.prompt)['raw']
            reply = AnchorReply(statements=[Statement(text='atomic original background', sources=[next(iter(raw))])])
            return reply, SimpleNamespace(id=f'fake-{len(self.calls)}')

    gateway = Gateway()
    old = world.sessions.info('A')
    assert anchors.update(old, gateway=gateway, account=None, now=T0)
    assert anchors.readable('u1', 'page:p1')
    fresh = info('fresh')
    world.sessions.create(fresh)
    carried = carry_over(world.sessions, fresh, anchors=anchors)
    assert 'atomic original background' in carried.page_summary
    ports = MemoryPorts(world.mem, None, world.sessions, anchors=anchors)
    recalled = recall_block(ports, info=fresh.model_copy(update={'page_id': 'p2'}), query='atomic',
                            page_id='p2', concept_ids=(), state=fold(fresh, []),
                            known_concepts=frozenset(), now=T0)
    assert recalled.message and recalled.recalled[0]['id'].startswith('dream:anchor:')
    source = ReadPastTurnTool(ports).run({'anchor_id': 'page:p1', 'version': 1},
                                       ToolContext(user_id='u1', session_id='fresh'))
    assert source.data['source_nodes'] and source.data['preview_only']
    before = len(gateway.calls)
    assert anchors.update(old, gateway=gateway, account=None, now=T0)
    assert len(gateway.calls) == before
    world.sessions.append('A', 'late-rating', 'rating', {'message_id': 'a1-a', 'turn_id': 'a1', 'value': 'down', 'note': 'too shallow'}, T0)
    assert anchors.update(old, gateway=gateway, account=None, now=T0)
    assert len(gateway.calls) == before + 1 and 'too shallow' in gateway.calls[-1].prompt
    request = world.service.request_session('u1', 'A', requested_by='test')
    assert anchors.readable('u1', 'page:p1') is None
    assert world.service.run(request).state == 'committed'
    assert anchors.readable('u1', 'page:p1') is None
