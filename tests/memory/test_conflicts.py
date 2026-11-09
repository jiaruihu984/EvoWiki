"""Possibly conflicting memories (memory/conflicts.py): the library a check reads, the model's
groups as code checks them, the scan hash that skips an unchanged library, settling a group
(keep one: the others out of force their own normal way; keep both), the routes, and the forget
flow deleting the groups that name what it removes. Real Postgres, scripted model: no paid call."""

from datetime import timedelta

import pytest

from evowiki.assistant.contracts import MemoryConflictResolveIn
from evowiki.llm.gateway import LLMParseError
from evowiki.memory.conflicts import (
    CONFLICTS_PURPOSE,
    RESOLVE_KIND,
    ConflictIn,
    ConflictResolveExecutor,
    ConflictService,
    checked,
    drop_with,
)
from evowiki.memory.contracts import EvidenceRef, MemoryScope
from evowiki.memory.store import Item, PgMemoryStore
from evowiki.profile.note import WHO, line_key
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.preferences import land_preference
from tests.assistant.test_stated_preferences import ctx, own_items, prefs_env, remember
from tests.memory import forget_world
from tests.wikiapp.script import Script
from tests.wikiapp.test_review_flow import LATER

RULE = "以后都先给例子再讲定义"
RULE2 = "讲定义前不要举例子"
CARD = "讲定义时不要用比喻"
LINE = "我每天只有半小时"


def _service(env) -> ConflictService:
    return ConflictService(env.pool, env.repo, env.profile, clock=lambda: LATER, new_id=env.ids)


def _decision(env, item_id="mem_d1", text="这周每天学两小时", status="active"):
    PgMemoryStore(env.pool).put_item(Item(
        item_id=item_id, user_id="u1", kind="decision", content=text,
        provenance="explicit_history",
        evidence=(EvidenceRef(message_id="m5", session_id="s1", role="user", quote="每天学两小时"),),
        scope_type="page", scope_id="p1", page_id="p1", session_id="s1", valid_from=LATER,
        created_at=LATER, status=status))


@pytest.fixture
def lib(pool):
    """Two rules of his (chat), a card from consolidation (not in force), a profile line, an
    active decision; the undo of 「保留这条」 registered on his ledger."""
    env = prefs_env(pool)
    env.pool = pool
    for said in (RULE, RULE2):
        assert remember(env, ctx(said), quote=said, text=said).error is None
    card = land_preference(env.repo, env.repo.load("u1"), kind="teaching", text=CARD,
                           provenance="explicit_history",
                           evidence=(EvidenceRef(message_id="m2", session_id="s0", role="user",
                                                 quote="讲定义的时候别用比喻"),),
                           scope=MemoryScope(scope_type="global"), session_id=None,
                           idempotency_key="c1", now=LATER, new_id=env.ids)
    env.profile.update("u1", lambda p: p.model_copy(update={"owned": {WHO: (LINE,)}}))
    _decision(env)
    _decision(env, "mem_old", "旧决定", status="superseded")
    mine = {i.text: i.item_id for i in own_items(env)}
    env.rule, env.rule2 = mine[RULE], mine[RULE2]
    env.card = card
    env.line = line_key(WHO, LINE)
    env.ledger.register(RESOLVE_KIND, ConflictResolveExecutor(
        pool, env.repo, env.profile, clock=lambda: LATER, new_id=env.ids))
    return env


def _script(*replies):
    s = Script()
    for r in replies:
        s.push(CONFLICTS_PURPOSE, {"conflicts": r})
    return s


def test_the_library_holds_what_is_in_force_for_him(lib):
    entries = {e.id: e for e in _service(lib).library("u1")}
    assert {e.category for e in entries.values()} == {"rule", "profile", "decision"}
    rule = entries[lib.rule]
    assert (rule.category, rule.source, rule.text) == ("rule", "chat", RULE)
    assert rule.date is not None and rule.origin_ref == "s1/m1" and RULE in (rule.quote or "")
    assert lib.card.item_id not in entries                  # a card waiting for him: not in force
    line = entries[lib.line]
    assert (line.source, line.section, line.where, line.where_kind) == ("profile", WHO, None, None)
    d = entries["mem_d1"]
    assert (d.source, d.where, d.where_kind, d.origin_ref, d.quote) == (
        "consolidation", "p1", "page", "s1/m5", "每天学两小时")       # no such page: its id
    assert "mem_old" not in entries                         # superseded: not in force
    rsi = [i for i in tspec.effective_spec(lib.state()).items if i.authority == "rsi"]
    assert rsi and not any(i.item_id in entries for i in rsi)  # RSI's rules are not his


def test_code_checks_the_groups():
    ids = {"a", "b", "c"}
    got = checked([ConflictIn(ids=["b", "a"], why="x"), ConflictIn(ids=["a", "a"], why="one"),
                   ConflictIn(ids=["a", "zz"], why="unknown"), ConflictIn(ids=["a", "b"], why="dup"),
                   ConflictIn(ids=["b", "c"], why="kept both before")],
                  ids, [frozenset({"b", "c"})])
    assert got == [(("a", "b"), "x")]


def test_a_check_stores_the_clear_groups_and_an_unchanged_library_is_not_read_again(lib):
    s = _script([{"ids": [lib.rule, lib.rule2], "why": "一条要先举例，一条不要举例"},
                 {"ids": [lib.rule, lib.card.item_id], "why": "卡片还没生效"}])
    svc = _service(lib)
    calls = svc.check("u1", s.gateway(), None)
    assert len(calls) == 1
    (req,) = s.requests(CONFLICTS_PURPOSE)
    assert RULE in req.prompt and LINE in req.prompt and CARD not in req.prompt
    assert "能同时成立" in req.system
    [g] = svc.open("u1")
    assert g.why == "一条要先举例，一条不要举例"
    assert {i.id for i in g.items} == {lib.rule, lib.rule2}
    assert svc.check("u1", _script().gateway(), None) == []     # same library: no call


def test_one_contradiction_stays_one_group_as_the_library_grows(lib):
    svc = _service(lib)
    svc.check("u1", _script([{"ids": [lib.rule, lib.rule2], "why": "两条"}]).gateway(), None)
    _decision(lib, "mem_d2", "新的决定")
    svc.check("u1", _script([{"ids": [lib.rule, lib.rule2, "mem_d2"], "why": "三条"}]).gateway(), None)
    assert [g.why for g in svc.open("u1")] == ["三条"]
    _decision(lib, "mem_d3", "又一条")
    svc.check("u1", _script([{"ids": [lib.rule, lib.rule2], "why": "两条"}]).gateway(), None)
    assert [g.why for g in svc.open("u1")] == ["三条"]


def test_kept_both_is_not_raised_again(lib):
    pair = {"ids": [lib.rule, lib.rule2], "why": "矛盾"}
    svc = _service(lib)
    svc.check("u1", _script([pair]).gateway(), None)
    [g] = svc.open("u1")
    assert svc.resolve("u1", g.conflict_id, keep_both=True) == ("kept_both", None)
    assert svc.open("u1") == []
    _decision(lib, "mem_d2", "新的决定")                         # the library changed
    svc.check("u1", _script([pair]).gateway(), None)
    assert svc.open("u1") == []
    with pytest.raises(LookupError):
        svc.resolve("u1", g.conflict_id, keep_both=True)         # settled already


def test_keep_one_takes_the_others_out_and_its_undo_puts_them_back(lib):
    svc = _service(lib)
    svc.check("u1", _script([{"ids": [lib.rule, lib.rule2, lib.line, "mem_d1"],
                              "why": "都在说怎么安排"}]).gateway(), None)
    [g] = svc.open("u1")
    with pytest.raises(ValueError):
        svc.resolve("u1", g.conflict_id, keep="mem_nope")
    status, action_id = svc.resolve("u1", g.conflict_id, keep=lib.rule)
    assert status == "resolved"
    assert [i.text for i in own_items(lib)] == [RULE, CARD]          # rule2 out, the card untouched
    assert {d.rule_id: d.action for d in lib.state().rule_decisions}[lib.rule2] == "revoked"
    assert lib.profile.get("u1").owned.get(WHO, ()) == ()             # line removed
    d = PgMemoryStore(lib.pool).get_item("u1", "mem_d1")
    assert d.status == "superseded" and d.status_history[-1]["by"] == "conflict"
    assert svc.open("u1") == []
    row = lib.ledger.get("u1", action_id)
    assert (row.kind, row.status, row.source) == (RESOLVE_KIND, "executed", "user")
    assert RULE in row.summary and set(row.content["others"]) == {lib.rule2, lib.line, "mem_d1"}

    lib.ledger.undo("u1", action_id)                                   # 撤销
    assert lib.ledger.get("u1", action_id).status == "undone"
    assert sorted(i.text for i in own_items(lib)) == sorted([RULE, RULE2, CARD])
    assert lib.state().rule_decisions[-1].action == "reaccepted"
    assert lib.profile.get("u1").owned[WHO] == (LINE,)
    d = PgMemoryStore(lib.pool).get_item("u1", "mem_d1")
    assert d.status == "active" and d.status_history[-1]["by"] == "conflict_undo"
    [again] = svc.open("u1")                                           # the group is open again
    assert again.conflict_id == g.conflict_id


def test_keeping_a_decision_revokes_his_rule(lib):
    svc = _service(lib)
    svc.check("u1", _script([{"ids": [lib.rule, "mem_d1"], "why": "x"}]).gateway(), None)
    [g] = svc.open("u1")
    svc.resolve("u1", g.conflict_id, keep="mem_d1")
    assert RULE not in [i.text for i in own_items(lib)]
    assert {d.rule_id: d.action for d in lib.state().rule_decisions}[lib.rule] == "revoked"
    assert PgMemoryStore(lib.pool).get_item("u1", "mem_d1").status == "active"


def test_an_undo_after_a_later_change_is_refused(lib):
    from evowiki.bot.actions import NotDecidable
    svc = _service(lib)
    svc.check("u1", _script([{"ids": ["mem_d1", lib.line], "why": "x"}]).gateway(), None)
    [g] = svc.open("u1")
    _, action_id = svc.resolve("u1", g.conflict_id, keep=lib.line)
    PgMemoryStore(lib.pool).set_item_status("mem_d1", "resolved", at=LATER, by="test")
    with pytest.raises(NotDecidable):
        lib.ledger.undo("u1", action_id)
    assert lib.ledger.get("u1", action_id).status == "executed"
    assert svc.open("u1") == []


def test_resolve_body_takes_one_of_keep_or_keep_both():
    assert MemoryConflictResolveIn(keep="a").keep == "a"
    assert MemoryConflictResolveIn(keep_both=True).keep_both
    for bad in ({}, {"keep": "a", "keep_both": True}):
        with pytest.raises(ValueError):
            MemoryConflictResolveIn(**bad)


# ---- forgetting removes the groups naming what goes ----

def _group(pool, cid, ids):
    with pool.connection() as conn:
        conn.execute("insert into memory_conflict (conflict_id, user_id, item_ids, why, status, "
                     "detected_at) values (%s, 'u1', %s, '矛盾', 'open', %s)",
                     (cid, sorted(ids), forget_world.T0))


def _left(pool):
    with pool.connection() as conn:
        return sorted(r["conflict_id"] for r in conn.execute(
            "select conflict_id from memory_conflict").fetchall())


def test_forgetting_a_session_or_an_item_drops_its_groups(pool, tmp_path):
    w = forget_world.build(pool, tmp_path)
    _group(pool, "g_a", ["item_a", "si9"])          # an item of the deleted session
    _group(pool, "g_rule", ["si1", "si9"])          # a rule he ticked to revoke with it
    _group(pool, "g_b1", ["item_b1", "si9"])        # a clean item: kept
    assert w.service.forget_session("u1", "A", revoke_rule_ids=["si1"],
                                    requested_by="test").state == "committed"
    assert _left(pool) == ["g_b1"]
    assert w.service.forget_item("u1", "item_b1", requested_by="test") == "committed"
    assert _left(pool) == []


def test_drop_with_touches_only_his_groups(pool, tmp_path):
    forget_world.build(pool, tmp_path)
    _group(pool, "g1", ["x", "y"])
    with pool.connection() as conn:
        assert drop_with(conn, "u2", ["x"]) == 0 and drop_with(conn, "u1", []) == 0
        assert drop_with(conn, "u1", ["y", "q"]) == 1


def test_check_after_a_failed_call_reads_again(lib):
    svc = _service(lib)
    s = Script().push(CONFLICTS_PURPOSE, "不是 JSON")
    with pytest.raises(LLMParseError):
        svc.check("u1", s.gateway(), None)
    calls = svc.check("u1", _script([]).gateway(), None)
    assert len(calls) == 1                               # the hash was not stored


def test_dates_come_from_the_spec_history(lib):
    e = next(x for x in _service(lib).library("u1") if x.id == lib.rule)
    assert abs(e.date - LATER) < timedelta(days=400)


class _Checker:
    def __init__(self, fail=False):
        self.users, self.fail = [], fail

    def check(self, user_id, gateway, account):
        self.users.append(user_id)
        if self.fail:
            raise RuntimeError("model down")
        return []


def test_replace_with_a_profile_line_lands_it_and_undo_takes_it_back(lib):
    from evowiki.memory.conflicts import Replacement
    from evowiki.profile.note import WANT

    svc = _service(lib)
    svc.check("u1", _script([{"ids": [lib.line, "mem_d1"], "why": "时间对不上"}]).gateway(), None)
    [g] = svc.open("u1")
    said = "我平时每天一小时"
    status, action_id = svc.resolve("u1", g.conflict_id, replace_with=Replacement(
        text=said, quote=said, origin_ref="s1/m9", section=WANT))
    assert status == "resolved"
    p = lib.profile.get("u1")
    assert p.owned.get(WANT) == (said,) and p.owned.get(WHO, ()) == ()
    meta = p.fact_meta[line_key(WANT, said)]
    assert (meta.provenance, meta.origin_ref, meta.action_id) == ("explicit_current", "s1/m9",
                                                                 action_id)
    assert PgMemoryStore(lib.pool).get_item("u1", "mem_d1").status == "superseded"
    lib.ledger.undo("u1", action_id)
    p = lib.profile.get("u1")
    assert p.owned.get(WANT, ()) == () and p.owned[WHO] == (LINE,)
    assert PgMemoryStore(lib.pool).get_item("u1", "mem_d1").status == "active"
    assert [x.conflict_id for x in svc.open("u1")] == [g.conflict_id]
    with pytest.raises(ValueError):
        svc.resolve("u1", g.conflict_id, replace_with=Replacement(
            text="x", quote="x", origin_ref="s1/m9", section="怎么学比较好"))   # rules now


def test_only_who_and_want_lines_are_profile_entries(lib):
    from evowiki.profile.note import HOW, WANT
    lib.profile.update("u1", lambda p: p.model_copy(update={"owned": {
        WHO: (LINE,), WANT: ("想学统计",), HOW: ("喜欢先看例子",), "已经会的": ("会求导",)}}))
    lines = {e.text: e.section for e in _service(lib).library("u1") if e.category == "profile"}
    assert lines == {LINE: WHO, "想学统计": WANT}


def test_where_is_the_bare_domain_or_the_page_title_and_the_source_names_its_chat(lib, pool):
    from psycopg.types.json import Jsonb

    from evowiki.profile.note import FactMeta
    from evowiki.wikiapp.preferences import DELETED
    assert remember(lib, ctx("数学题都先解释符号", mid="m4"), quote="数学题都先解释符号",
                    text="数学题都先解释符号", domain="数学").error is None
    _decision(lib, "mem_pg", "这一页用数字例子")
    with pool.connection() as conn:
        conn.execute("update memory_item set scope_id = %s where item_id = 'mem_pg'", (lib.pid,))
        conn.execute("insert into assistant_session (session_id, user_id, created_at) "
                     "values ('s1', 'u1', %s) on conflict do nothing", (LATER,))
        conn.execute("insert into assistant_entry (session_id, seq, entry_id, type, payload, "
                     "occurred_at) values ('s1', 1, 'e1', 'user', %s, %s)",
                     (Jsonb({"message_id": "m1", "text": "讲定义的顺序？\n第二行"}), LATER))
    lib.profile.update("u1", lambda p: p.model_copy(update={"fact_meta": {
        lib.line: FactMeta(provenance="explicit_current", origin_ref=f"{DELETED}s0/m2")}}))
    entries = {e.text: e for e in _service(lib).library("u1")}
    math = entries["数学题都先解释符号"]
    assert (math.where, math.where_kind) == ("数学", "domain")
    page = entries["这一页用数字例子"]
    assert page.where_kind == "page" and page.where == lib.state().pages[lib.pid].title
    rule = entries[RULE]
    assert (rule.origin_title, rule.origin_deleted) == ("讲定义的顺序", False)
    gone = entries[LINE]
    assert (gone.origin_title, gone.origin_deleted) == (None, True)
    assert entries["这周每天学两小时"].origin_title == "讲定义的顺序"   # its words were in s1 too


def test_a_group_the_model_says_can_hold_together_is_not_raised():
    from evowiki.memory.conflicts import ConflictIn, checked

    both = ConflictIn(ids=["a", "b"], why="学过高数和不会 Python 不一定矛盾", can_both_hold=True)
    assert checked([both], {"a", "b"}, []) == []
