"""B3 acceptance: one user, the real flows and workflows (DBOS on the throwaway database), the HTTP
routes of /wiki/bot/* and /wiki/rsi/*. No model, no paid API.

Day 1 (08:30 local): 3 negative explanation signals, 3 different wrong answers, a goal whose
page was not opened for 4 days, and a review card due. `bot_check` then `bot_daily` run."""
from datetime import UTC, datetime, timedelta

import pytest
from dbos import DBOS, SetWorkflowID
from fastapi.testclient import TestClient

from evowiki.agent import dbos_app
from evowiki.assistant.pg_store import PgSessionStore
from evowiki.assistant.session import SessionInfo
from evowiki.belief.aspects import DefineConcept, Observe
from evowiki.bot import durable
from evowiki.bot.actions import ActionLedger
from evowiki.bot.bot_schemas import ActivityOut, SuggestionOut, WhyOut
from evowiki.bot.change_source import LedgerChangeSource
from evowiki.bot.durable import BotRuntime
from evowiki.bot.executors import PreferenceExecutor
from evowiki.goals.contracts import (
    Aspect,
    Observation,
    OutlineSection,
    PageOutline,
    SectionRole,
    SectionText,
)
from evowiki.goals.fold import Page
from evowiki.service.app import create_app
from evowiki.service.rsi_runs import bot_rsi_runner
from evowiki.store import bot as store
from evowiki.store.rsi import PgRsiStore
from evowiki.teach.contracts import ChangeOut, RsiSettings, Signal
from evowiki.wikiapp.events import (
    PageOpened,
    PageRecord,
    PageSaved,
    PathRecord,
    PathSaved,
    SignalRecorded,
    WikiEvent,
)
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.runtime.helpers import make_user
from tests.service.conftest import H
from tests.service.conftest import services as _services_fixture
from tests.wikiapp.test_k1_state import lp

services = _services_fixture
DAY1 = datetime(2026, 10, 14, 0, 30, tzinfo=UTC)         # 08:30 in Asia/Shanghai: inside the window
DAY2 = DAY1 + timedelta(days=1)
SLOT1 = "bot:slot:daily:u1:2026-10-14"
SLOT2 = "bot:slot:daily:u1:2026-10-15"


def seed_rest(pool) -> None:
    """User u1 with the page pg_1 (the goal's page), a review page, 3 negative signals and a goal."""
    make_user(pool, "u1")
    repo = PgRepo(pool)
    first = PageRecord(page_id="pg_1", title="页1", goal="学统计",
                       concepts=("c1",), targets=("c1",), created_at=DAY1 - timedelta(days=9),
                       page=Page(outline=PageOutline(
                           page_id="pg_1", goal="学统计", concepts=("c1",), targets=("c1",),
                           sections=(OutlineSection(id="s1", heading="页1：怎么读",
                                                    role=SectionRole.EXPLAIN,
                                                    cells=("c1.interpret",)),)), states={}))
    outline = PageOutline(
        page_id="pg_rev", goal="学统计", concepts=("c2",), targets=("c2",),
        sections=(OutlineSection(id="s1", heading="方差怎么算", role=SectionRole.EXPLAIN,
                                 cells=("c2.procedure",)),))
    rev = PageRecord(page_id="pg_rev", title="方差", goal="学统计",
                     concepts=("c2",), targets=("c2",), created_at=DAY1 - timedelta(days=70),
                     page=Page(outline=outline, states={}, texts={"s1": SectionText(
                         section_id="s1", cue="方差 = 偏差平方的均值", answer="a", body="b", cache_key="k")}))
    path = PathRecord(path=lp(2), page_ids=("pg_1", None),
                      created_at=DAY1 - timedelta(days=5))
    signals = [WikiEvent(event_id=f"wev_sig{i}", ts=DAY1 - timedelta(hours=5 - i),
                         body=SignalRecorded(signal=Signal(
                             signal_id=f"sig{i}", kind="section_deeper",
                             at=DAY1 - timedelta(hours=5 - i)))) for i in range(3)]
    repo.commit(repo.load("u1"), Commit(
        aspect=[DefineConcept(event_id="ae_def1", ts=DAY1 - timedelta(days=80), concept_id="c1",
                              aspects=(Aspect.INTERPRET, Aspect.PROCEDURE)),
                *(Observe(event_id=f"ae_w{i}", ts=DAY1 - timedelta(hours=3 - i),
                          observation=Observation(cell="c1.interpret", polarity="~", strength=0.8,
                                                  kind="check", evidence_ref=f"check:pg_1:q{i}"))
                  for i in range(3)),                            # three distinct questions qualify for remedial help
                DefineConcept(event_id="ae_def2", ts=DAY1 - timedelta(days=80), concept_id="c2",
                              aspects=(Aspect.PROCEDURE,)),
                *(Observe(event_id=f"ae_rv{i}", ts=DAY1 - timedelta(days=60, minutes=-i),
                          observation=Observation(cell="c2.procedure", polarity="+", strength=1.0,
                                                  kind="quiz", evidence_ref=f"quiz:rv{i}"))
                  for i in range(3))],
        wiki=[WikiEvent(event_id="wev_pg1", ts=DAY1 - timedelta(days=9),
                        body=PageSaved(record=first, action="created")),
              WikiEvent(event_id="wev_rev", ts=DAY1 - timedelta(days=70),
                        body=PageSaved(record=rev, action="created")),
              WikiEvent(event_id="wev_path", ts=DAY1 - timedelta(days=5),
                        body=PathSaved(record=path, action="page_started")),
              WikiEvent(event_id="wev_open", ts=DAY1 - timedelta(days=4),
                        body=PageOpened(page_id="pg_1")), *signals]))


class World:
    def __init__(self, services, client, tmp_path):
        self.services, self.client, self.pool = services, client, services.pool

    def get(self, path, **params):
        r = self.client.get(path, headers=H("u1"), params=params)
        assert r.status_code == 200, r.text
        return r.json()

    def post(self, path, body=None):
        r = self.client.post(path, headers=H("u1"), json=body)
        assert r.status_code == 200, r.text
        return r.json()

    def cards(self, **params):
        return [SuggestionOut.model_validate(c)
                for c in self.get("/wiki/bot/suggestions", **params)]

    def recent(self, **params):
        return [ActivityOut.model_validate(a) for a in self.get("/wiki/bot/recent", **params)]

    def tick(self, wid, at):
        self.services.clock.t = at
        with SetWorkflowID(wid):
            return durable.bot_tick(at, None)

    def n_rows(self):
        with self.pool.connection() as conn:
            return tuple(conn.execute(f"select count(*) as n from {t}").fetchone()["n"]
                         for t in ("bot_trigger", "bot_suggestion", "bot_run"))


@pytest.fixture
def world(services, db_url, tmp_path):
    pool = services.pool
    seed_rest(pool)
    services.clock.t = DAY1
    with pool.connection() as conn:           # he is there on every day of this world
        store.mark_seen(conn, "u1", DAY1 + timedelta(days=60))
    ledger = ActionLedger(pool, services.clock, services.new_id,
                          {"preference": PreferenceExecutor(pool, services.clock)})
    services.actions = ledger
    services.change_sources = (LedgerChangeSource(ledger),)
    w = World(services, TestClient(create_app(services), base_url="http://127.0.0.1"), tmp_path)
    dbos_app.launch(db_url, app_version=dbos_app.APP_VERSION)
    durable.register(BotRuntime(pool, services.clock, services.new_id,
                                rsi=bot_rsi_runner(services)), schedule=False)
    yield w
    dbos_app.shutdown()


def day1(w: World) -> dict:
    """bot_check (events) then the daily slot; returns the trigger ids by kind."""
    ids = durable.enqueue(durable.bot_check, "u1", "bot:check:u1:acc", "u1").get_result()
    for tid in ids:
        if DBOS.list_workflows(workflow_id_prefix=durable.rsi_id(tid, 0)):
            DBOS.retrieve_workflow(durable.rsi_id(tid, 0)).get_result()
    w.tick("acc-tick-1", DAY1)
    daily = DBOS.retrieve_workflow(SLOT1).get_result()
    with w.pool.connection() as conn:
        kinds = {t.kind: t for t in store.triggers_since(conn, "u1", DAY1 - timedelta(hours=1))}
    assert len(ids) == 2 and len(daily) == 4  # strategy + remedial; daily includes the weekly review
    return kinds


def test_the_expected_cards_and_the_strategy_hand_off(world):
    w = world
    kinds = day1(w)
    assert set(kinds) == {"strategy_negative", "wrong_answers", "review_due", "goal_idle", "goal_review"}
    strategy = kinds["strategy_negative"]
    assert (strategy.status, strategy.route, strategy.gate_outcome) == ("handed_off", "rsi", "pass")
    # RSI is off until the user turns it on (settings switch): the Bot runs no cycle
    assert strategy.gate_detail == "讲解调整设为手动，这次没有自动调整"
    assert strategy.run_id == durable.rsi_id(strategy.trigger_id, 0)
    with w.pool.connection() as conn:
        assert conn.execute("select count(*) as n from rsi_cycle where user_id = 'u1'"
                            ).fetchone()["n"] == 0

    cards = {c.kind: c for c in w.cards()}
    assert set(cards) == {"wrong_answers", "review_due", "goal_idle"}  # no card for the RSI hand-off
    remedial = cards["wrong_answers"]
    assert remedial.action["type"] == "open_page" and remedial.action["page_id"] == "pg_1"
    assert "3 道不同的题答错了" in remedial.action["ask"]
    assert cards["review_due"].action == {"type": "start_practice"}
    assert cards["review_due"].title == "复习 1 张卡片"
    assert cards["goal_idle"].action == {"type": "open_page", "page_id": "pg_1"}
    assert all(c.status == "shown" and c.placement == "today" for c in cards.values())
    assert w.get("/wiki/bot/badge") == {"today_new": 3, "pending_actions": 0}   # not reacted yet

    acts = {a.kind: a for a in w.recent()}
    assert {k: a.result for k, a in acts.items()} == {
        "wrong_answers": "suggested", "review_due": "suggested", "goal_idle": "suggested",
        "strategy_negative": "handed_to_rsi", "goal_review": "reviewed"}


def test_with_a_cycle_on_the_strategy_hand_off_runs_one_of_its_own(world):
    w = world
    PgRsiStore(w.pool).put_settings("u1", RsiSettings(cycle="every_10_feedback", harness=False))
    strategy = day1(w)["strategy_negative"]
    assert (strategy.status, strategy.route, strategy.gate_outcome) == ("handed_off", "rsi", "pass")
    # B5c: the Bot ran one cycle of its own (the harness off: no round)
    assert strategy.gate_detail == "讲解的自动改进没有打开，这次没有改"
    assert strategy.run_id == durable.rsi_id(strategy.trigger_id, 0)
    with w.pool.connection() as conn:
        cyc = conn.execute("select trigger, result ->> 'bot_trigger_id' as t from rsi_cycle "
                           "where user_id = 'u1'").fetchall()
    assert [(c["trigger"], c["t"]) for c in cyc] == [("bot", strategy.trigger_id)]


def test_the_why_chain_of_a_card_is_complete_and_follows_the_reaction(world):
    w = world
    kinds = day1(w)
    card = next(c for c in w.cards() if c.kind == "review_due")
    why = WhyOut.model_validate(w.get("/wiki/bot/why", suggestion_id=card.suggestion_id))
    assert (why.trigger.kind, why.trigger.rule_id) == ("review_due", "review_due")
    assert why.trigger.reason
    assert why.gate.outcome == "pass" and why.gate.reason is None
    assert why.gate.detail.startswith("今日第 ")
    assert [(o.type, o.id, o.status) for o in why.outputs] == [
        ("suggestion", card.suggestion_id, "shown")]
    assert why.reaction.status == "shown" and why.reaction.reacted_at is None
    # the same chain by trigger id
    by_trigger = WhyOut.model_validate(
        w.get("/wiki/bot/why", trigger_id=kinds["review_due"].trigger_id))
    assert by_trigger.outputs == why.outputs

    done = SuggestionOut.model_validate(w.post(
        f"/wiki/bot/suggestions/{card.suggestion_id}/react", {"reaction": "accepted"}))
    assert done.status == "accepted"
    after = WhyOut.model_validate(w.get("/wiki/bot/why", suggestion_id=card.suggestion_id))
    assert after.reaction.status == "accepted" and after.reaction.reacted_at == DAY1
    assert after.outputs[0].status == "accepted"
    assert after.trigger == why.trigger and after.gate == why.gate     # the chain above is unchanged

    # the chain of any card is trigger -> gate -> output -> reaction
    goal = next(c for c in w.cards() if c.kind == "goal_idle")
    g = WhyOut.model_validate(w.get("/wiki/bot/why", suggestion_id=goal.suggestion_id))
    assert g.trigger.kind == "goal_idle" and g.gate.outcome == "pass"
    assert [o.id for o in g.outputs] == [goal.suggestion_id] and g.reaction.status == "shown"


def test_the_same_slot_twice_changes_nothing(world):
    w = world
    day1(w)
    before = (w.n_rows(), [c.suggestion_id for c in w.cards()])
    assert w.tick("acc-tick-2", DAY1) == []                       # same scheduled_at, new workflow id
    h = durable.enqueue(durable.bot_daily, "u1", SLOT1, "u1", "2026-10-14", DAY1.isoformat())
    assert len(h.get_result()) == 4                               # the first run's result, nothing re-ran
    again = durable.enqueue(durable.bot_check, "u1", "bot:check:u1:acc-2", "u1").get_result()
    assert again == []                                            # the cursor is past every event
    assert (w.n_rows()[:2], [c.suggestion_id for c in w.cards()]) == (before[0][:2], before[1])
    # one more daily for the same local date under another workflow id: the triggers' source keys
    # dedup, so no trigger and no card is added
    extra = durable.enqueue(durable.bot_daily, "u1", "bot:slot:daily:u1:again", "u1",
                            "2026-10-14", DAY1.isoformat()).get_result()
    assert extra == [] and w.n_rows()[:2] == before[0][:2]


def test_a_kind_turned_off_yields_a_kind_off_skip_visible_only_with_skipped(world):
    w = world
    day1(w)
    settings = w.get("/wiki/bot/settings")
    assert settings["kinds"]["goal_idle"] is True
    off = {**settings, "kinds": {**settings["kinds"], "goal_idle": False}}
    r = w.client.put("/wiki/bot/settings", headers=H("u1"), json=off)
    assert r.status_code == 200 and r.json()["kinds"]["goal_idle"] is False

    w.tick("acc-tick-day2", DAY2)
    assert len(DBOS.retrieve_workflow(SLOT2).get_result()) == 3     # remedial + review_due + goal_idle
    default = w.recent(days=2)
    assert all(a.result != "skipped" for a in default)
    assert "goal_idle" not in {a.kind for a in default if a.at >= DAY2}
    with_skipped = [a for a in w.recent(days=2, skipped=True) if a.kind == "goal_idle"
                    and a.result == "skipped"]
    [skip] = with_skipped
    assert (skip.gate_outcome, skip.gate_reason, skip.gate_detail) == (
        "skip", "kind_off", "「继续目标」已关闭")
    assert skip.suggestion_title is None and skip.cost_usd == 0
    # no goal card was made on day 2; the review card was (its day-1 card has expired)
    today = w.cards()
    assert "goal_idle" not in {c.kind for c in today if c.created_at >= DAY2}
    assert any(c.kind == "review_due" and c.created_at >= DAY2 for c in today)


def test_a_page_chip_not_interested_the_preference_row_and_its_undo(world):
    w = world
    day1(w)
    # one open question on pg_1 (compaction summary of an assistant session)
    sessions = PgSessionStore(w.pool)
    sessions.create(SessionInfo(session_id="as1", user_id="u1", page_id="pg_1",
                                note_concept_id=None, created_at=DAY1 - timedelta(days=1)))
    sessions.append("as1", "e1", "compaction", {"summary": "还没懂的：标准差的含义"},
                    DAY1 - timedelta(hours=2))
    assert w.cards(placement="page", page_id="pg_1") == []        # page chips were removed
    assert w.client.get("/wiki/bot/suggestions", headers=H("u1"),
                        params={"placement": "page"}).status_code == 422

    goal = next(c for c in w.cards() if c.kind == "goal_idle")
    assert goal.preference_action_id is None
    out = SuggestionOut.model_validate(w.post(
        f"/wiki/bot/suggestions/{goal.suggestion_id}/react", {"reaction": "not_interested"}))
    assert out.status == "not_interested" and out.preference_action_id.startswith("act_")
    assert goal.suggestion_id not in {c.suggestion_id for c in w.cards()}

    changes = [ChangeOut.model_validate(c) for c in w.get("/wiki/rsi/changes")]
    [row] = [c for c in changes if c.patch_id == out.preference_action_id]
    assert (row.track, row.status, row.can_undo, row.target) == (
        "G", "personal_accepted", True, "知识库")
    assert row.summary.startswith("以后少提醒「") and row.why == "你点了「不感兴趣」"

    undone = ChangeOut.model_validate(w.post(f"/wiki/rsi/patches/{row.patch_id}/undo"))
    assert (undone.status, undone.can_undo) == ("rolled_back", False)
    back = {c.suggestion_id: c for c in w.cards()}
    assert back[goal.suggestion_id].status == "shown"             # the card is back
    assert back[goal.suggestion_id].preference_action_id is None
    row_after = next(ChangeOut.model_validate(c) for c in w.get("/wiki/rsi/changes")
                     if c["patch_id"] == row.patch_id)
    assert row_after.status == "rolled_back"
