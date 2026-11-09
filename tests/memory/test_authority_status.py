"""Task 11: a preference / fact's status is read from its authority (S3's draft, the teaching
spec, the profile) at query time and never written back; history search labels it. In-memory
stores."""

from datetime import timedelta

from evowiki.memory.contracts import EvidenceRef
from evowiki.memory.search import authority_status, search
from evowiki.memory.store import InMemoryMemoryStore, Item
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.teach import remove_item_flow, take_over_flow
from tests.memory.test_lander import IDS, pending_rule
from tests.wikiapp.test_goal import NOW

U = "u1"
PROFILE = {"关于我": ("我是高二学生",)}


def _item(iid, kind, content, **kw) -> Item:
    return Item(item_id=iid, user_id=U, kind=kind, content=content,
                provenance="explicit_history",
                evidence=(EvidenceRef(message_id="m3", session_id="s1", role="user",
                                      quote=content),),
                scope_type="global", session_id="s1", valid_from=NOW - timedelta(days=1),
                created_at=NOW, **kw)


def _world():
    repo = MemoryRepo([U])
    pending_rule(repo, "数学题先解释符号", "mb1")
    mem = InMemoryMemoryStore()
    pref = _item("i1", "preference", "数学题先解释符号", spec_item_id="mb1")
    fact = _item("i2", "about_me", "我是高二学生",
                 profile_ref={"section": "关于我", "text": "我是高二学生"})
    mem.put_item(pref)
    mem.put_item(fact)
    return repo, mem, pref, fact


def _labels(repo, mem, profile):
    def authority(uid):
        state = repo.load(uid).state
        return lambda it: authority_status(it, state, profile)
    hits = search(mem, None, U, "数学题先解释符号 高二学生", mode="history", authority=authority)
    return {h.id: h.label for h in hits}


def test_status_follows_the_spec_and_the_profile():
    repo, _, pref, fact = _world()
    assert authority_status(pref, repo.load(U).state, PROFILE) == "pending"
    assert authority_status(fact, repo.load(U).state, PROFILE) == "active"
    assert authority_status(fact, repo.load(U).state, {"关于我": ()}) == "gone"
    take_over_flow(repo, U, "mb1", now=NOW, new_id=IDS)
    assert authority_status(pref, repo.load(U).state, PROFILE) == "active"
    remove_item_flow(repo, U, "mb1", now=NOW, new_id=IDS)
    assert authority_status(pref, repo.load(U).state, PROFILE) == "gone"


def test_a_draft_waiting_for_him_follows_its_ledger_row():
    item = _item("i3", "preference", "例子都用生活里的", pending_action_id="act1")
    assert authority_status(item, None, {}) == "pending"          # no ledger to ask
    for row, want in (("proposed", "pending"), ("executed", "active"), ("rejected", "gone"),
                      ("undone", "gone"), (None, "gone")):
        assert authority_status(item, None, {}, lambda aid, r=row: r) == want


def test_history_labels_pending_and_deleted_and_never_writes_back():
    repo, mem, _, _ = _world()
    assert _labels(repo, mem, PROFILE) == {"item:i1": "待确认，未生效", "item:i2": ""}
    remove_item_flow(repo, U, "mb1", now=NOW, new_id=IDS)            # he deletes the rule
    assert not any(i.item_id == "mb1" for i in tspec.effective_spec(repo.load(U).state).items)
    assert _labels(repo, mem, {}) == {"item:i1": "已删除，未生效", "item:i2": "已删除，未生效"}
    assert [i.status for i in mem.items(U)] == [None, None]          # never written back


def test_auto_mode_never_returns_them_and_an_unread_authority_is_unchecked():
    _, mem, *_ = _world()
    assert search(mem, None, U, "数学题先解释符号", mode="auto") == []
    hits = search(mem, None, U, "数学题先解释符号", mode="history")
    assert [h.label for h in hits] == ["没能核对是否生效"]

    def broken(uid):
        raise RuntimeError("down")
    hits = search(mem, None, U, "数学题先解释符号", mode="history", authority=broken)
    assert [h.label for h in hits] == ["没能核对是否生效"]
