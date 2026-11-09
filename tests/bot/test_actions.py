import threading
from datetime import UTC, datetime, timedelta

import pytest

from evowiki.agent.effects import EffectConflict
from evowiki.bot.actions import (
    STALE_DETAIL,
    ActionLedger,
    ActionRefused,
    ActionRow,
    NotDecidable,
    level_for,
)
from evowiki.bot.settings import DEFAULT_SETTINGS, BotSettings
from evowiki.store import bot as store
from evowiki.wikiapp.events import UsageAdded, UsageRecord, WikiEvent
from evowiki.wikiapp.repo import Commit, PgRepo
from tests.runtime.helpers import make_user

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
_n = iter(range(100_000))


def new_id(prefix):
    return f"{prefix}_{next(_n)}"


class Clock:
    def __init__(self):
        self.t = NOW

    def __call__(self):
        return self.t


class FakeExecutor:
    """Commits one real wiki event with the ledger's effect in Commit.extra."""

    def __init__(self, pool, *, refuse: ActionRefused | None = None, crash_after: bool = False):
        self.repo, self.refuse, self.crash_after = PgRepo(pool), refuse, crash_after
        self.commits, self.rejects, self.undos = 0, [], []

    def execute(self, user_id, action, effect):
        if self.refuse is not None:
            raise self.refuse
        ev = WikiEvent(event_id=new_id("wev"), ts=NOW,
                       body=UsageAdded(record=UsageRecord(action="act", text=action.action_id)))
        self.repo.commit(self.repo.load(user_id), Commit(wiki=[ev], extra=[effect]))
        self.commits += 1
        if self.crash_after:
            raise RuntimeError("crash after the commit")
        return {"done": True, "wiki_event_ids": [ev.event_id]}

    def rejected(self, user_id, action):
        self.rejects.append(action.action_id)

    def undo(self, user_id, action):
        self.undos.append(action.action_id)
        return {}


ASK_PAGES = BotSettings(levels={"page_body": "ask"})     # these tests exercise the `ask` path


@pytest.fixture
def env(pool):
    make_user(pool, "u1")
    make_user(pool, "u2")
    with pool.connection() as conn:
        store.put_settings(conn, "u1", ASK_PAGES, NOW)
    clock = Clock()
    ex = FakeExecutor(pool)
    return pool, clock, ex, ActionLedger(pool, clock, new_id, {"assistant_edit": ex})


def propose(ledger, key="k1", **kw):
    base = {"kind": "assistant_edit", "scope": "page_body", "source": "assistant",
            "summary": "改一段", "why": "因为", "content": {"x": 1}, "key": key}
    return ledger.propose("u1", **{**base, **kw})


def wiki_events(pool):
    with pool.connection() as conn:
        return conn.execute("select count(*) as n from wiki_event where user_id = 'u1' and type = 'usage_added'").fetchone()["n"]


def test_approve_twice_executes_once(env):
    pool, _, ex, ledger = env
    a = propose(ledger)
    assert a.status == "proposed" and a.action_id.startswith("act_") and a.level == "ask"
    before = wiki_events(pool)
    done = ledger.approve("u1", a.action_id)
    assert done.status == "executed" and done.result == {"done": True, "wiki_event_ids": [
        done.wiki_event_ids[0]]} and len(done.wiki_event_ids) == 1
    again = ledger.approve("u1", a.action_id)
    assert again.status == "executed" and ex.commits == 1
    assert wiki_events(pool) == before + 1


def test_crash_after_commit_leaves_executed_and_no_second_event(env):
    pool, clock, _, _ = env
    ex = FakeExecutor(pool, crash_after=True)
    ledger = ActionLedger(pool, clock, new_id, {"assistant_edit": ex})
    a = propose(ledger)
    before = wiki_events(pool)
    with pytest.raises(RuntimeError):
        ledger.approve("u1", a.action_id)
    with pool.connection() as conn:
        assert store.get_action(conn, a.action_id).status == "executed"
    assert ledger.approve("u1", a.action_id).status == "executed"
    assert ex.commits == 1 and wiki_events(pool) == before + 1


def test_crash_before_execute_leaves_approved_and_retry_runs(env):
    pool, _, ex, ledger = env
    a = propose(ledger)
    with pool.connection() as conn:
        store.set_action_status(conn, a.action_id, status="approved", at=NOW)
    assert ledger.approve("u1", a.action_id).status == "executed"
    assert ex.commits == 1


def test_stale_page_fails_the_action(env):
    pool, clock, _, _ = env
    ex = FakeExecutor(pool, refuse=ActionRefused("x", stale=True))
    ledger = ActionLedger(pool, clock, new_id, {"assistant_edit": ex})
    a = propose(ledger)
    before = wiki_events(pool)
    r = ledger.approve("u1", a.action_id)
    assert r.status == "failed" and r.detail == STALE_DETAIL
    assert wiki_events(pool) == before
    with pytest.raises(NotDecidable):
        ledger.approve("u1", a.action_id)


def test_refusal_detail_is_kept(env):
    pool, clock, _, _ = env
    ex = FakeExecutor(pool, refuse=ActionRefused("这一段已经不存在"))
    ledger = ActionLedger(pool, clock, new_id, {"assistant_edit": ex})
    r = ledger.approve("u1", propose(ledger).action_id)
    assert r.status == "failed" and r.detail == "这一段已经不存在"


def test_never_scope_and_non_ask_scope(env):
    _, _, _, ledger = env
    with pytest.raises(PermissionError):
        level_for("user_notes", DEFAULT_SETTINGS)
    with pytest.raises(PermissionError):
        propose(ledger, scope="user_notes")
    with pytest.raises(ValueError, match="record_in"):
        propose(ledger, scope="structure")
    assert level_for("page_body", ASK_PAGES) == "ask"          # the user's setting decides
    assert level_for("page_body", DEFAULT_SETTINGS) == "auto_undo"
    assert level_for("structure", ASK_PAGES) == "auto_undo"
    assert ledger.level("u1", "page_body") == "ask"
    assert ledger.level("u2", "page_body") == "auto_undo"      # u2 has no stored settings


def test_same_key_other_content_conflicts_same_content_dedups(env):
    pool, _, _, ledger = env
    a = propose(ledger)
    assert propose(ledger).action_id == a.action_id
    with pytest.raises(EffectConflict):
        propose(ledger, content={"x": 2})
    with pool.connection() as conn:
        assert len(store.list_actions(conn, "u1")) == 1


def test_a_pending_proposal_never_expires(env):
    pool, clock, ex, ledger = env
    a = propose(ledger)
    assert a.expires_at is None
    clock.t = NOW + timedelta(days=400)
    with pool.connection() as conn:
        assert store.count_pending(conn, "u1") == 1
    assert [r.action_id for r in ledger.list("u1", status="proposed")] == [a.action_id]
    assert not hasattr(ledger, "expire")
    assert ledger.approve("u1", a.action_id).status == "executed" and ex.commits == 1


def test_a_click_approval_needs_auto_undo_and_runs_at_once(env):
    pool, _, ex, ledger = env
    with pytest.raises(ValueError, match="auto_undo"):
        propose(ledger, approved_now=True)                  # u1's page_body level is ask
    with pool.connection() as conn:
        store.put_settings(conn, "u1", DEFAULT_SETTINGS, NOW)
    a = propose(ledger, approved_now=True)
    assert a.status == "approved" and a.level == "auto_undo" and a.expires_at is None
    with pool.connection() as conn:
        assert store.count_pending(conn, "u1") == 0         # never shown as pending
    assert ledger.approve("u1", a.action_id).status == "executed" and ex.commits == 1


def test_reject_calls_executor_and_blocks_approve(env):
    _, _, ex, ledger = env
    a = propose(ledger)
    assert ledger.reject("u1", a.action_id).status == "rejected"
    assert ex.rejects == [a.action_id]
    with pytest.raises(NotDecidable):
        ledger.approve("u1", a.action_id)
    done = ledger.approve("u1", propose(ledger, key="k2").action_id)
    with pytest.raises(NotDecidable):
        ledger.reject("u1", done.action_id)


def test_reject_survives_executor_error(env):
    pool, clock, _, _ = env

    class Bad(FakeExecutor):
        def rejected(self, user_id, action):
            raise RuntimeError("boom")

    ledger = ActionLedger(pool, clock, new_id, {"assistant_edit": Bad(pool)})
    a = propose(ledger)
    assert ledger.reject("u1", a.action_id).status == "rejected"


def test_other_users_action_is_404(env):
    _, _, _, ledger = env
    a = propose(ledger)
    for op in (ledger.approve, ledger.reject, ledger.undo):
        with pytest.raises(LookupError):
            op("u2", a.action_id)


def test_undo_auto_undo_executed_row(env):
    pool, _, ex, ledger = env
    with pool.connection() as conn, conn.transaction():
        ledger.record_in(conn, "u1", kind="assistant_edit", scope="structure", source="assistant",
                         summary="合并", content={"m": 1}, key="auto1", wiki_event_ids=["e1"])
    with pool.connection() as conn:
        [row] = store.list_actions(conn, "u1")
    assert row.status == "executed" and row.level == "auto_undo" and row.wiki_event_ids == ["e1"]
    assert ledger.undo("u1", row.action_id).status == "undone"
    assert ex.undos == [row.action_id]
    with pytest.raises(NotDecidable):
        ledger.undo("u1", row.action_id)


def test_undo_refused_is_409_and_proposed_not_undoable(env):
    pool, clock, _, _ = env

    class NoUndo(FakeExecutor):
        def undo(self, user_id, action):
            raise ActionRefused("已经改过了")

    ledger = ActionLedger(pool, clock, new_id, {"assistant_edit": NoUndo(pool)})
    with pytest.raises(NotDecidable):
        ledger.undo("u1", propose(ledger).action_id)
    done = ledger.approve("u1", propose(ledger, key="k2").action_id)
    with pytest.raises(NotDecidable, match="已经改过了"):
        ledger.undo("u1", done.action_id)
    with pool.connection() as conn:
        assert store.get_action(conn, done.action_id).status == "executed"


def test_record_in_upserts_and_set_status_in(env):
    pool, _, _, ledger = env
    kw = {"kind": "rsi_patch", "scope": "rsi", "source": "rsi", "summary": "s",
          "content": {"p": 1}, "key": "r1"}
    with pool.connection() as conn, conn.transaction():
        ledger.record_in(conn, "u1", status="proposed", **kw)
        ledger.record_in(conn, "u1", result={"ok": 1}, undo={"u": 1}, **kw)
        ledger.set_status_in(conn, "r1", "undone", detail="撤销", at=NOW)
        row = store.get_action_by_key(conn, "r1")
    assert row.status == "undone" and row.result == {"ok": 1} and row.undo == {"u": 1}
    assert row.detail == "撤销"
    with pool.connection() as conn:
        assert len(store.list_actions(conn, "u1")) == 1
    with pytest.raises(PermissionError), pool.connection() as conn:
        ledger.record_in(conn, "u1", **{**kw, "scope": "user_notes", "key": "r2"})


def test_second_concurrent_approve_rolls_its_wiki_write_back(env):
    pool, clock, _, _ = env
    gate = threading.Event()

    class Gated(FakeExecutor):
        def __init__(self, pool):
            super().__init__(pool)
            self.calls = 0
            self.entered = threading.Event()

        def execute(self, user_id, action, effect):
            self.calls += 1
            if self.calls == 1:              # request B: passed step 1, waits for A to finish
                self.entered.set()
                assert gate.wait(10)
            return super().execute(user_id, action, effect)

    ex = Gated(pool)
    ledger = ActionLedger(pool, clock, new_id, {"assistant_edit": ex})
    a = propose(ledger)
    with pool.connection() as conn:
        store.set_action_status(conn, a.action_id, status="approved", at=NOW)
    out = {}
    t = threading.Thread(target=lambda: out.update(b=ledger.approve("u1", a.action_id)))
    t.start()
    assert ex.entered.wait(10)
    assert ledger.approve("u1", a.action_id).status == "executed"     # A
    gate.set()
    t.join(10)
    assert out["b"].status == "executed"
    assert wiki_events(pool) == 1 and ex.commits == 1


def test_concurrent_undos_run_the_executor_once(env):
    pool, clock, _, _ = env
    release = threading.Event()

    class Slow(FakeExecutor):
        def __init__(self, pool):
            super().__init__(pool)
            self.entered = threading.Event()

        def undo(self, user_id, action):
            self.entered.set()
            assert release.wait(10)
            return super().undo(user_id, action)

    ex = Slow(pool)
    ledger = ActionLedger(pool, clock, new_id, {"assistant_edit": ex})
    done = ledger.approve("u1", propose(ledger).action_id)
    out = {}

    def second():
        try:
            out["r"] = ledger.undo("u1", done.action_id)
        except NotDecidable as e:
            out["r"] = e

    first = threading.Thread(target=lambda: out.update(a=ledger.undo("u1", done.action_id)))
    first.start()
    assert ex.entered.wait(10)
    t2 = threading.Thread(target=second)
    t2.start()
    release.set()
    first.join(10)
    t2.join(10)
    assert out["a"].status == "undone"
    assert ex.undos == [done.action_id]
    assert isinstance(out["r"], NotDecidable) or out["r"].status == "undone"


def test_missing_executor_is_501_and_row_stays_proposed(env):
    pool, clock, _, _ = env
    ledger = ActionLedger(pool, clock, new_id, {})
    a = propose(ledger)
    with pytest.raises(NotImplementedError):
        ledger.approve("u1", a.action_id)
    with pool.connection() as conn:
        assert store.get_action(conn, a.action_id).status == "proposed"


def test_executor_that_never_runs_effect_fails_closed(env):
    pool, clock, _, _ = env

    class Lazy(FakeExecutor):
        def execute(self, user_id, action, effect):
            return None

    ledger = ActionLedger(pool, clock, new_id, {"assistant_edit": Lazy(pool)})
    r = ledger.approve("u1", propose(ledger).action_id)
    assert r.status == "failed" and r.detail == "这条改动没有成功应用"


def test_undo_of_auto_level_is_refused(env):
    pool, _, ex, ledger = env
    with pool.connection() as conn, conn.transaction():
        ledger.record_in(conn, "u1", kind="assistant_edit", scope="suggestion", source="bot",
                         summary="s", content={"a": 1}, key="auto2")
    with pool.connection() as conn:
        [row] = store.list_actions(conn, "u1")
    with pytest.raises(NotDecidable):
        ledger.undo("u1", row.action_id)
    assert ex.undos == []


def test_executed_ask_row_undo_goes_to_executor(env):
    _, _, ex, ledger = env
    done = ledger.approve("u1", propose(ledger).action_id)
    assert ledger.undo("u1", done.action_id).status == "undone" and ex.undos == [done.action_id]


def test_upsert_never_touches_another_users_key(env):
    pool, _, _, ledger = env
    kw = {"kind": "rsi_patch", "scope": "rsi", "source": "rsi", "summary": "s",
          "content": {"p": 1}, "key": "shared"}
    with pool.connection() as conn, conn.transaction():
        ledger.record_in(conn, "u1", **kw)
    with pytest.raises(ValueError, match="another user"), pool.connection() as conn:
        ledger.record_in(conn, "u2", status="undone", **kw)
    with pool.connection() as conn:
        assert store.get_action_by_key(conn, "shared").status == "executed"


def test_page_fingerprint():
    from evowiki.wikiapp.events import PageRecord
    from evowiki.wikiapp.page_version import page_fingerprint
    assert page_fingerprint(None) == "absent"
    rec = PageRecord(page_id="p", title="t", goal="g", concepts=(), targets=(),
                     created_at=NOW)
    assert page_fingerprint(rec) == "absent"
    from evowiki.goals.contracts import OutlineSection, PageOutline
    from evowiki.goals.fold import Page
    sec = OutlineSection(id="s1", heading="h", role="explain")
    outline = PageOutline(page_id="p", goal="g", concepts=("c1",), targets=("c1",),
                          sections=(sec,))
    page = Page(outline=outline, states={})
    a = rec.model_copy(update={"page": page})
    same = rec.model_copy(update={"page": Page(outline=outline, states={})})
    changed = rec.model_copy(update={"page": page.model_copy(
        update={"unresolved_conflicts": ("x",)})})
    assert page_fingerprint(a) == page_fingerprint(same) != "absent"
    assert page_fingerprint(changed) != page_fingerprint(a)


def _row(kind, *, status="executed", level="auto_undo", content=None, undo=None):
    return ActionRow(
        action_id="act_x", user_id="u1", kind=kind, scope="page_body", level=level, status=status,
        source="bot", summary="s", why="", content=content or {}, content_hash="h",
        idempotency_key="k", created_at=NOW, undo=undo)


@pytest.mark.parametrize("kind,content,undo,expected", [
    ("assistant_edit", {}, {"section_id": "a"}, True),      # asked or auto: undo data decides
    ("assistant_edit", {}, None, False),
    ("preference", {}, None, True),
    ("rsi_patch", {}, None, True),          # the executor undoes the RSI change itself
    ("rsi_method", {}, None, True),
    ("something_else", {}, {"u": 1}, True),      # unregistered kind: undo data decides
    ("something_else", {}, None, False),
])
def test_can_undo_by_kind(kind, content, undo, expected):
    from evowiki.bot.actions import can_undo
    for level in ("ask", "auto_undo"):          # the level plays no part
        assert can_undo(_row(kind, level=level, content=content, undo=undo)) is expected
    for status in ("proposed", "approved", "rejected", "failed", "undone"):
        assert can_undo(_row(kind, status=status, content=content, undo=undo)) is False


def test_both_surfaces_use_the_same_can_undo():
    from evowiki.bot.bot_schemas import ActionOut
    from evowiki.bot.change_source import change_out
    cases = [_row("assistant_edit", level="ask", undo={"s": 1}),     # ask-level edit is undoable
             _row("assistant_edit", undo=None), _row("rsi_patch"), _row("preference")]
    for r in cases:
        assert ActionOut.of(r).can_undo == change_out(r).can_undo == can_undo_of(r)


def test_action_out_section_id_is_where_the_change_is():
    from evowiki.bot.bot_schemas import ActionOut
    # an executed edit: the section its undo restores; a proposal: the one its content names; else none
    assert ActionOut.of(_row("assistant_edit", content={"section_id": "s1"},
                             undo={"section_id": "s9"})).section_id == "s9"
    assert ActionOut.of(_row("assistant_edit", status="proposed",
                             content={"section_id": "s1"})).section_id == "s1"
    assert ActionOut.of(_row("page_merge", content={"section_id": 3})).section_id is None
    assert ActionOut.of(_row("page_merge")).section_id is None


def can_undo_of(r):
    from evowiki.bot.actions import can_undo
    return can_undo(r)


def test_a_pending_row_waiting_for_the_user_carries_the_level_in_force(env):
    pool, _, _, ledger = env
    with pool.connection() as conn:
        store.put_settings(conn, "u1", BotSettings(levels={"structure": "ask"}), NOW)
    with pool.connection() as conn, conn.transaction():
        ledger.record_in(conn, "u1", kind="path_edit", scope="structure", source="assistant",
                         summary="s", content={"op": "move"}, key="edit-pending:u1:p1",
                         status="proposed")
        ledger.record_in(conn, "u1", kind="path_edit", scope="structure", source="assistant",
                         summary="s", content={"op": "move"}, key="edit:u1:e1")
    with pool.connection() as conn:
        assert store.get_action_by_key(conn, "edit-pending:u1:p1").level == "ask"
        assert store.get_action_by_key(conn, "edit:u1:e1").level == "auto_undo"


def test_an_approved_row_stays_in_the_pending_list_and_count(env):
    pool, clock, _, ledger = env
    a = propose(ledger)
    with pool.connection() as conn:
        store.set_action_status(conn, a.action_id, status="approved", at=NOW)   # 429 / crash
        assert store.count_pending(conn, "u1", NOW) == 0       # being executed right now
    clock.t = NOW + timedelta(minutes=10)
    with pool.connection() as conn:
        assert store.count_pending(conn, "u1", clock.t) == 1
    assert [r.action_id for r in ledger.list("u1", status="proposed")] == [a.action_id]
    assert [r.action_id for r in ledger.list("u1", status="approved")] == [a.action_id]
    assert ledger.approve("u1", a.action_id).status == "executed"     # approving again works
    with pool.connection() as conn:
        assert store.count_pending(conn, "u1") == 0
