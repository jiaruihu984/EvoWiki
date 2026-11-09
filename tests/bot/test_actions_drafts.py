"""Task 1 of assistant-actions: drafts the user edits before 确认, strictest level, cost on ledger
rows, per-kind undo registry."""

from datetime import UTC, datetime

import pytest

from evowiki.agent.effects import payload_hash
from evowiki.bot.actions import (
    ActionLedger,
    ActionRefused,
    ActionRow,
    NotDecidable,
    can_undo,
    register_undo,
    strictest,
)
from evowiki.bot.bot_schemas import ActionOut
from evowiki.bot.settings import BotSettings
from evowiki.store import bot as store
from evowiki.wikiapp.events import UsageAdded, UsageRecord, WikiEvent
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)
_n = iter(range(100_000))


def new_id(prefix):
    return f"{prefix}_{next(_n)}"


class DraftExecutor:
    """Revises `content["text"]` from the draft and executes with the stored content."""

    def __init__(self, pool, *, refuse: bool = False):
        self.repo, self.refuse = PgRepo(pool), refuse
        self.revised, self.executed_with = [], []

    def revise(self, action, draft):
        self.revised.append((action.action_id, draft))
        if self.refuse:
            raise ActionRefused("草稿不合法")
        return {**action.content, "text": draft["text"]}

    def execute(self, user_id, action, effect):
        self.executed_with.append(action.content)
        ev = WikiEvent(event_id=new_id("wev"), ts=NOW,
                       body=UsageAdded(record=UsageRecord(action="act", text=action.action_id)))
        self.repo.commit(self.repo.load(user_id), Commit(wiki=[ev], extra=[effect]))
        return {"wiki_event_ids": [ev.event_id]}

    def rejected(self, user_id, action): ...

    def undo(self, user_id, action):
        return {}


class PlainExecutor(DraftExecutor):
    revise = None                      # an executor without `revise`


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    with pool.connection() as conn:
        store.put_settings(conn, "u1", BotSettings(levels={"page_body": "ask"}), NOW)
    ex = DraftExecutor(pool)
    plain = PlainExecutor(pool)
    ledger = ActionLedger(pool, lambda: NOW, new_id, {"draft_kind": ex, "plain_kind": plain})
    return pool, ledger, ex, plain


def propose(ledger, kind="draft_kind", key="k1", **kw):
    base = {"kind": kind, "scope": "page_body", "source": "assistant", "summary": "s", "why": "",
            "content": {"text": "old"}, "key": key}
    return ledger.propose("u1", **{**base, **kw})


def test_strictest_ordering():
    assert strictest(["auto", "auto_undo"]) == "auto_undo"
    assert strictest(["auto_undo", "ask", "auto"]) == "ask"
    assert strictest(["ask", "never"]) == "never"
    assert strictest(iter(["auto"])) == "auto"
    with pytest.raises(ValueError):
        strictest([])


def test_approve_with_draft_revises_stores_and_executes_with_it(env):
    _, ledger, ex, _ = env
    row = propose(ledger)
    out = ledger.approve("u1", row.action_id, draft={"text": "new"})
    assert out.status == "executed"
    assert out.content == {"text": "new"}
    assert out.content_hash == payload_hash({"text": "new"})
    assert ex.revised == [(row.action_id, {"text": "new"})]
    assert ex.executed_with == [{"text": "new"}]


def test_revise_refused_fails_the_row_and_executes_nothing(env):
    pool, ledger, _, _ = env
    ex = DraftExecutor(pool, refuse=True)
    ledger.register("draft_kind", ex)
    row = propose(ledger)
    out = ledger.approve("u1", row.action_id, draft={"text": "new"})
    assert out.status == "failed" and out.detail == "草稿不合法"
    assert out.content == {"text": "old"}
    assert ex.executed_with == []


def test_draft_on_a_kind_without_revise_is_not_decidable(env):
    _, ledger, _, plain = env
    row = propose(ledger, kind="plain_kind", key="k2")
    with pytest.raises(NotDecidable):
        ledger.approve("u1", row.action_id, draft={"text": "x"})
    assert plain.executed_with == []
    assert ledger.list("u1", status="proposed")[0].status == "proposed"
    # approving without a draft still works
    assert ledger.approve("u1", row.action_id).status == "executed"


def test_action_out_confirm_and_cost():
    def row(**kw):
        base = {"action_id": "a", "user_id": "u1", "kind": "k", "scope": "page_body",
                "level": "ask", "status": "executed", "source": "assistant", "summary": "s",
                "content": {}, "content_hash": "h", "idempotency_key": "k", "created_at": NOW}
        return ActionRow(**{**base, **kw})
    assert ActionOut.of(row(content={"confirm": True})).confirm is True
    assert ActionOut.of(row()).confirm is False
    assert ActionOut.of(row(result={"cost_usd": 0.0123})).cost_usd == 0.0123
    assert ActionOut.of(row(result={"done": 1})).cost_usd is None
    assert ActionOut.of(row()).cost_usd is None


def test_can_undo_uses_the_registered_rule():
    def row(kind, **kw):
        return ActionRow(action_id="a", user_id="u1", kind=kind, scope="page_body", level="ask",
                         status="executed", source="x", summary="s", content={}, content_hash="h",
                         idempotency_key="k", created_at=NOW, **kw)
    assert can_undo(row("t1_new_kind")) is False                     # default: needs undo data
    assert can_undo(row("t1_new_kind", undo={"u": 1})) is True
    register_undo("t1_new_kind", lambda r: r.content.get("ok", False))
    assert can_undo(row("t1_new_kind", undo={"u": 1})) is False      # the rule replaces the default
    assert can_undo(ActionRow(**{**row("t1_new_kind").__dict__, "content": {"ok": True}})) is True
    assert can_undo(ActionRow(**{**row("t1_new_kind").__dict__, "content": {"ok": True},
                                 "status": "proposed"})) is False
