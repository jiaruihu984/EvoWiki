"""The pages an automatic rule change leaves out of date (`wikiapp.rule_stale`, the retired RSI-B's
`mark_stale` made generic for the harness loop): a change marks every written page it affects (an
added rule or a param: all of them; an edited / removed rule: the pages written with it in force),
once; an undo that restores what a page was written under clears the mark; changes saved before
the cutoff, or of an origin it does not follow, are compared but mark nothing. MemoryRepo +
scripted page writer; the change's ops come from a dict (`ops_of`). No network."""

from datetime import timedelta

from evowiki.teach import spec_ops
from evowiki.teach.contracts import ParamPatchOp, SpecPatchOp
from evowiki.wikiapp import rule_stale
from evowiki.wikiapp.rule_stale import mark_stale, population
from evowiki.wikiapp.spec_store import RepoSpecRegistry
from evowiki.wikiapp.stale import stale_pages
from evowiki.wikiapp.state import replay
from tests.wikiapp.test_goal import NOW
from tests.wikiapp.test_organize import add_page, run, script_page
from tests.wikiapp.test_page import env, organized  # noqa: F401 — fixture

U = "u1"
SINCE = NOW - timedelta(days=1)
ADD = SpecPatchOp(op="add_item", item_id="h-1", text="每节先给一个能看到结果的小例子。")
PARAM = ParamPatchOp(track="B", path="analogy_level", value=2)


def _pages(env, n=3) -> list[str]:  # noqa: F811
    pids = [organized(env)]
    for k in range(n - 1):
        pid = add_page(env[0], env[2], title=f"第 {k + 2} 页")
        script_page(env[1])
        run(env, pid)
        pids.append(pid)
    return pids


class Changes:
    """Commits RSI changes as the harness round does (origin "harness:<id>") and answers
    `ops_of` for them."""

    def __init__(self, env):  # noqa: F811
        self.repo, self.ids = env[0], env[2]
        self.reg = RepoSpecRegistry(self.repo, self.ids)
        self.ops: dict[str, tuple] = {}
        self.before: dict[str, object] = {}

    def apply(self, rid: str, *ops):
        cur = self.reg.current(U).spec
        new = cur
        for op in ops:
            new = spec_ops.apply(new, op, patch_id=f"harness:{rid}")
        self.ops[f"harness:{rid}"], self.before[rid] = ops, cur
        self.reg.commit(U, new, patch_id=f"harness:{rid}", at=NOW)

    def undo(self, rid: str):
        spec = self.reg.current(U).spec
        for op in reversed(self.ops[f"harness:{rid}"]):
            spec = spec_ops.revert(spec, self.before[rid], op)
        self.reg.commit(U, spec, patch_id=f"harness:{rid}", at=NOW, undo=True)

    def ops_of(self, user_id, origin):
        return self.ops.get(origin)

    def mark(self, since=SINCE) -> int:
        return mark_stale(self.repo, self.ids, U, ops_of=self.ops_of, now=NOW, since=since)


def state(env):  # noqa: F811
    return env[0].load(U).state


def test_an_added_rule_marks_every_written_page_once_and_the_marks_replay(env):  # noqa: F811
    pids = _pages(env)
    ch = Changes(env)
    ch.apply("r1", ADD)
    assert ch.mark() == len(pids)
    st = state(env)
    assert set(stale_pages(st)) == set(pids)
    assert {m.patch_id for m in stale_pages(st).values()} == {"harness:r1"}
    assert st.stale_checked_version == 2
    assert ch.mark() == 0                                  # idempotent
    assert replay(env[0]._wiki[U]).to_json() == state(env).to_json()


def test_an_undo_back_to_the_written_rules_clears_the_marks(env):  # noqa: F811
    _pages(env)
    ch = Changes(env)
    ch.apply("r1", PARAM)
    assert ch.mark() == 3
    ch.undo("r1")
    assert ch.mark() == 0
    st = state(env)
    assert stale_pages(st) == {} and st.stale_marks == {} and st.stale_checked_version == 3


def test_an_edit_marks_only_the_pages_written_with_that_rule(env):  # noqa: F811
    pids = _pages(env)
    snap = env[0].load(U)
    used = {i for g in snap.state.section_logs[pids[0]].values() for i in g.spec_items}
    assert "d04" in used                                   # an "any" rule every page used
    assert population(snap, (SpecPatchOp(op="edit_item", item_id="d04", text="x"),)) == pids
    assert population(snap, (SpecPatchOp(op="remove_item", item_id="h-9"),)) == []
    assert population(snap, (PARAM,)) == pids
    ch = Changes(env)
    ch.apply("r1", SpecPatchOp(op="edit_item", item_id="d04", text="新词先用一个生活里的例子讲清楚。"))
    assert ch.mark() == 3


def test_changes_before_the_cutoff_or_not_followed_are_compared_but_mark_nothing(env):  # noqa: F811
    _pages(env)
    ch = Changes(env)
    ch.apply("r1", ADD)
    assert ch.mark(since=NOW + timedelta(seconds=1)) == 0
    assert state(env).stale_checked_version == 2 and state(env).stale_marks == {}
    ch.apply("r2", PARAM)
    ch.ops.pop("harness:r2")                               # an origin ops_of does not follow
    assert ch.mark() == 0
    assert state(env).stale_checked_version == 3 and state(env).stale_marks == {}


def test_the_default_cutoff_is_read_at_call_time(env, monkeypatch):  # noqa: F811
    _pages(env)
    ch = Changes(env)
    ch.apply("r1", ADD)
    monkeypatch.setattr(rule_stale, "STALE_SINCE", SINCE)
    assert mark_stale(env[0], env[2], U, ops_of=ch.ops_of, now=NOW) == 3
