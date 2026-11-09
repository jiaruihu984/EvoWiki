"""Durable provenance traversal, publication fencing, and derived-copy erasure."""

import threading

import psycopg
import pytest

from evowiki.memory.forget import ForgetService, closure_batch, memory_reads_paused
from tests.memory.forget_world import T0, _trace, build


def test_more_than_ten_thousand_nodes_resume_after_process_restart(pool, tmp_path):
    w = build(pool, tmp_path)
    with pool.connection() as conn, conn.cursor() as cur:
        cur.executemany("insert into memory_dependency values('u1','session:A',%s,%s)",
                        [(f"copy:{i}", f"copy:{i}") for i in range(10_050)])
    rid = w.service.request_session("u1", "A", requested_by="test")
    with pool.connection() as conn, conn.transaction():
        assert not closure_batch(conn, rid, batch_size=1)
    with pool.connection() as conn:
        rows = conn.execute("select count(*) as n from forget_frontier where request_id=%s",
                            (rid,)).fetchone()["n"]
        assert rows > 10_000
        assert memory_reads_paused(conn, "u1")
    restarted = ForgetService(pool, w.ledger, spec=w.spec, harness=w.harness, clock=lambda: T0, new_id=lambda p: p)
    assert restarted.run(rid).state == "committed"
    assert restarted.run(rid).state == "committed"
    with pool.connection() as conn:
        assert not memory_reads_paused(conn, "u1")
        assert not conn.execute("select 1 from forget_frontier where request_id=%s "
                                "and not expanded", (rid,)).fetchone()
    assert w.turn_rows("D", "d1")[0]["payload"]["text"]


def test_request_waits_for_inflight_provenance_and_blocks_later_publish(pool, tmp_path):
    w = build(pool, tmp_path)
    entered, release, requested = threading.Event(), threading.Event(), threading.Event()
    failures = []
    box = {}

    def publish():
        try:
            with pool.connection() as conn, conn.transaction():
                conn.execute("insert into memory_dependency values('u1','session:A','copy:late','copy:late')")
                entered.set()
                assert release.wait(5)
        except (AssertionError, psycopg.Error) as exc:
            failures.append(exc)

    def request():
        try:
            assert entered.wait(5)
            box['rid'] = w.service.request_session("u1", "A", requested_by="test")
            requested.set()
        except (AssertionError, psycopg.Error) as exc:
            failures.append(exc)

    a, b = threading.Thread(target=publish), threading.Thread(target=request)
    a.start()
    b.start()
    try:
        assert entered.wait(5)
        assert not requested.wait(0.15)
    finally:
        release.set()
        a.join(5)
        b.join(5)
    assert not failures and requested.is_set()
    with pool.connection() as conn, pytest.raises(psycopg.errors.RaiseException):
        conn.execute("insert into assistant_entry(session_id,entry_id,seq,type,payload,occurred_at) "
                     "values('D','late-entry',999,'assistant','{\"message_id\":\"late\",\"text\":\"x\"}',%s)", (T0,))
    assert w.service.run(box['rid']).state == "committed"
    with pool.connection() as conn:
        assert conn.execute("select 1 from forget_frontier where request_id=%s and node='copy:late'",
                            (box['rid'],)).fetchone()


def test_summary_and_model_texts_are_purged_through_registered_call_nodes(pool, tmp_path):
    from evowiki.store.dreaming import publish, register_consumption

    w = build(pool, tmp_path)
    with pool.connection() as conn:
        assert publish(conn, 'u1', 'anchor', 'page:p1', expected_version=0,
                       payload={'summary': 'secret paraphrase'}, watermarks={'A': 2},
                       sources=['turn:A/a1'], fingerprint='f', now=T0)
    rid = w.service.request_session('u1', 'A', requested_by='test')
    with pool.connection() as conn, conn.transaction():
        while not closure_batch(conn, rid):
            pass
    # An already-running model can finish and register its provenance after traversal.
    with pool.connection() as conn:
        _trace(conn, None, 'dream-call', 'unrelated-subject', T0)
        register_consumption(conn, 'u1', 'call:dream-call', ['turn:A/a1'])
    assert w.service.run(rid).state == 'committed'
    with pool.connection() as conn:
        row = conn.execute("select payload,watermarks,dirty from dream_summary where user_id='u1'").fetchone()
        assert row['dirty'] and row['payload'] == {} and row['watermarks'] == {}
        for table, col in [('llm_call', 'id'), ('llm_request', 'call_id')]:
            assert not conn.execute(f"select 1 from {table} where {col}='dream-call'").fetchone()
        assert not conn.execute("select 1 from dream_source where user_id='u1'").fetchone()


def test_inflight_generation_prevents_commit_until_call_provenance_is_saved(pool, tmp_path):
    from evowiki.memory.forget import generation_guard
    from evowiki.store.dreaming import register_consumption

    w = build(pool, tmp_path)
    with generation_guard(pool, 'u1'):
        rid = w.service.request_session('u1', 'A', requested_by='test')
        outcome = w.service.run(rid)
        assert outcome.state == 'failed' and 'ForgetBusy' in outcome.error
        with pool.connection() as conn:
            _trace(conn, None, 'inflight-call', 'anchor:p1', T0)
            register_consumption(conn, 'u1', 'call:inflight-call', ['turn:A/a1'])
    assert w.service.run(rid).state == 'committed'
    with pool.connection() as conn:
        assert not conn.execute("select 1 from llm_call where id='inflight-call'").fetchone()

def test_reversible_suppression_hides_paraphrases_and_late_descendants(pool, tmp_path):
    from dataclasses import replace
    from types import SimpleNamespace

    from evowiki.memory.forget import PLACEHOLDER, sources_suppressed
    from evowiki.memory.forget_fact import MemoryForgetExecutor
    from evowiki.store.bot import ActionRow
    from evowiki.store.dreaming import publish, register_consumption

    w = build(pool, tmp_path)
    w.sessions.append('D', 'D-paraphrase', 'assistant',
                      {'message_id': 'd1-derived', 'text': 'An unnamed learned preference',
                       'recalled': [{'id': 'ep:A/a1'}]}, T0)
    with pool.connection() as conn:
        assert publish(conn, 'u1', 'anchor', 'page:p1', expected_version=0,
                       payload={'statements': [{'text': 'An unnamed learned preference'}]},
                       watermarks={'A': 2}, sources=['ep:A/a1'], fingerprint='f', now=T0)
    executor = MemoryForgetExecutor(pool, w.mem, SimpleNamespace(list=lambda *a, **kw: []), lambda: T0)
    action = ActionRow(action_id='soft-forget', user_id='u1', kind='memory_forget',
                       scope='suggestion', level='ask', status='executed', source='assistant',
                       summary='forget', content={'words': ['pandas']}, content_hash='h',
                       idempotency_key='soft', created_at=T0)
    result = executor.execute('u1', action, lambda conn: None)
    answers = [e.payload.get('text') for e in w.sessions.entries('D') if e.type == 'assistant']
    assert answers[-1] == PLACEHOLDER
    with pool.connection() as conn:
        assert sources_suppressed(conn, 'u1', ['turn:D/d1'])
        register_consumption(conn, 'u1', 'call:late-soft', ['turn:D/d1'])
        assert sources_suppressed(conn, 'u1', ['call:late-soft'])
        summary = conn.execute("select payload,dirty from dream_summary where user_id='u1'").fetchone()
        assert summary['dirty'] and summary['payload'] == {}
        assert conn.execute("select 1 from assistant_session where session_id='A'").fetchone()
    executor.undo('u1', replace(action, result=result))
    answers = [e.payload.get('text') for e in w.sessions.entries('D') if e.type == 'assistant']
    assert answers[-1] == 'An unnamed learned preference'
    with pool.connection() as conn:
        assert not sources_suppressed(conn, 'u1', ['turn:D/d1', 'call:late-soft'])
        assert conn.execute("select payload from dream_summary where user_id='u1'").fetchone()['payload'] == {}

def test_hard_delete_scrubs_round_that_only_read_a_derived_summary(pool, tmp_path):
    from evowiki.harness.forget import REDACTED
    from evowiki.store.dreaming import publish, register_consumption
    from tests.harness.test_forget import _call, _round, _run

    w = build(pool, tmp_path)
    with pool.connection() as conn:
        _round(conn, 'indirect', 'turn:assistant:D:d1', 'A private paraphrased conclusion')
        _round(conn, 'safe', 'turn:assistant:D:d1', 'Unrelated audit text')
        conn.execute("insert into harness_experience(exp_id,user_id,situation,change,evidence,"
                     "verdict,scope,created_at,weight) values('private-exp','u1',"
                     "'{\"text\":\"A private paraphrase\"}','{\"note\":\"A private paraphrase\"}',"
                     "'{\"round_id\":\"indirect\",\"note\":\"A private paraphrase\"}',"
                     "'effective','user',%s,1)", (T0,))
        conn.execute("insert into harness_gate(gate_id,user_id,round_id,subject,verdict,reasons,detail,at) "
                     "values('indirect-gate','u1','indirect','diff','pass',"
                     "'[\"A private paraphrase\"]','{\"note\":\"A private paraphrase\"}',%s)", (T0,))
        _call(conn, 'indirect-model', 'indirect', 't-indirect-model')
        _run(conn, 'harness:indirect:improver', 'indirect-model')
        assert publish(conn, 'u1', 'anchor', 'page:p1', expected_version=0,
                       payload={'statements': []}, watermarks={'A': 2},
                       sources=['turn:A/a1'], fingerprint='f', now=T0)
        register_consumption(conn, 'u1', 'round:indirect', ['dream:anchor:page:p1:1'])
    assert w.service.forget_session('u1', 'A', requested_by='test').state == 'committed'
    with pool.connection() as conn:
        row = conn.execute("select * from harness_round where round_id='indirect'").fetchone()
        assert row['status'] == 'applied' and row['reason'] == row['finding'] == REDACTED
        assert row['decision'] == {'source_deleted': True} and row['candidates'] == []
        assert conn.execute("select reason from harness_round where round_id='safe'").fetchone()['reason'] != REDACTED
        exp = conn.execute("select * from harness_experience where exp_id='private-exp'").fetchone()
        assert exp['situation'] == {'source_deleted': True} and exp['change'] == {}
        assert exp['evidence']['source_deleted'] and 'note' not in exp['evidence']
        gate = conn.execute("select * from harness_gate where gate_id='indirect-gate'").fetchone()
        assert gate['verdict'] == 'pass' and gate['detail'] == {'source_deleted': True}
        # These identifiers were cached before the delete and could otherwise be fed to a
        # model after the generation guard sees no pending request anymore.
        from evowiki.store.dreaming import sources_visible

        assert not sources_visible(conn, 'u1', ['session:A'])
        assert not sources_visible(conn, 'u1', ['round:indirect'])
        assert sources_visible(conn, 'u1', ['round:safe'])
        for table, col, key in [('agent_run', 'run_id', 'harness:indirect:improver'),
                                ('llm_call', 'id', 'indirect-model'),
                                ('llm_request', 'call_id', 'indirect-model'),
                                ('llm_text', 'hash', 't-indirect-model')]:
            assert not conn.execute(f"select 1 from {table} where {col}=%s", (key,)).fetchone()


def test_stale_snapshot_stays_ineligible_after_generation_guard_can_resume(pool, tmp_path):
    from evowiki.memory.forget import generation_guard
    from evowiki.store.dreaming import register_consumption, sources_visible

    w = build(pool, tmp_path)
    cached_sources = ['session:A', 'turn:A/a1', 'round:cached-consumer']
    with pool.connection() as conn:
        register_consumption(conn, 'u1', 'round:cached-consumer', ['turn:A/a1'])
        assert sources_visible(conn, 'u1', cached_sources)
    assert w.service.forget_session('u1', 'A', requested_by='test').state == 'committed'
    with generation_guard(pool, 'u1'), pool.connection() as conn:
        assert not memory_reads_paused(conn, 'u1')
        for source in cached_sources:
            assert not sources_visible(conn, 'u1', [source])
        assert sources_visible(conn, 'u1', ['session:D', 'turn:D/d1'])

@pytest.mark.parametrize('mode', ['json', 'chat'])
def test_failed_provider_output_and_trace_are_forgotten_without_a_returned_call(pool, tmp_path, mode):
    from pydantic import BaseModel

    from evowiki.llm import run_context
    from evowiki.llm.chat import ChatMessage, ChatRequest
    from evowiki.llm.gateway import Gateway, ProviderOutputError
    from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
    from evowiki.llm.scripted import ScriptedChatProvider
    from evowiki.memory.forget import generation_guard, taint_closure
    from evowiki.store.llm_logs import PgAttemptLog, PgCallLog
    from evowiki.store.trace import PgRequestLog, PgTraceSink

    class Answer(BaseModel):
        value: str

    w = build(pool, tmp_path)
    secret = 'private-origin-protected'

    def fail(*args):
        raise ProviderOutputError(secret, status='refusal', raw_output=secret, cost=0.0)

    provider = ScriptedChatProvider('scripted', fail, fail)
    ref = ModelRef(provider='scripted', model='m')
    gw = Gateway(GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True),
                 PgCallLog(pool), attempts=PgAttemptLog(pool), requests=PgRequestLog(pool),
                 providers={'scripted': provider}, mode='record', record_max_attempts=1,
                 clock=lambda: T0)
    run_context.configure(PgTraceSink(pool, lambda: T0))
    params = {'source_nodes': ['turn:A/a1'], 'source_user_id': 'u1'}
    with (generation_guard(pool, 'u1'),
          run_context.traced_step('memory_anchor', 'page:private', 'u1'),
          pytest.raises(ProviderOutputError)):
        if mode == 'json':
            gw.call('generator', LLMRequest(purpose='memory_anchor', prompt_version='test',
                    subject='page:private', system=secret, prompt=secret, params=params), Answer)
        else:
            gw.converse('generator', ChatRequest(purpose='memory_anchor', prompt_version='test',
                        subject='page:private', system=secret, tools=(),
                        messages=(ChatMessage(role='user', text=secret),), params=params))
    with pool.connection() as conn:
        failed = conn.execute("select id,raw_output from llm_attempt where subject='page:private'").fetchone()
        assert failed['raw_output'] == secret
        run = conn.execute("select run_id from agent_run where subject='page:private'").fetchone()['run_id']
        taint = taint_closure(conn, 'u1', 'A')
        assert 'attempt:' + failed['id'] in taint.paths and 'run:' + run in taint.paths
    assert w.service.forget_session('u1', 'A', requested_by='test').state == 'committed'
    with pool.connection() as conn:
        assert not conn.execute("select 1 from llm_attempt where id=%s", (failed['id'],)).fetchone()
        assert not conn.execute("select 1 from agent_step where run_id=%s", (run,)).fetchone()
        assert not conn.execute("select 1 from agent_run where run_id=%s", (run,)).fetchone()
        assert not conn.execute("select 1 from llm_text where text like %s", ('%' + secret + '%',)).fetchone()
