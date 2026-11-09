"""One store per thing (user ruling 2026-10-29): 我是谁 / 想学什么 in the profile, 怎么学比较好 in the
teaching spec, 已经会的 in K. The note shows them together; a line typed into a view goes to that
view's store, and the profile itself keeps only 我是谁 / 想学什么 lines."""

from datetime import UTC, datetime

import pytest

from evowiki.belief.aspects import Observe
from evowiki.memory.contracts import EvidenceRef, MemoryScope
from evowiki.profile.context import context_for
from evowiki.profile.note import GAPS, HOW, KNOWN, WANT, WHERE, WHO, Profile, parse, shown
from evowiki.profile.service import ProfileService
from evowiki.profile.store import MemoryProfileStore
from evowiki.wikiapp.guidelines import add_guideline, said_rules
from evowiki.wikiapp.preferences import land_preference
from evowiki.wikiapp.repo import MemoryRepo
from evowiki.wikiapp.wiki_schemas import GuidelineIn
from tests.wikiapp.script import Script
from tests.wikiapp.test_goal import Ids
from tests.wikiapp.test_page import page1

T = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


def _save(svc: ProfileService, text: str) -> str:
    """A save by the client that holds the last copy."""
    if not svc.version("u1"):
        svc.note("u1")
    return svc.edit("u1", text, base=svc.version("u1"))


def _k_service() -> tuple[MemoryRepo, ProfileService]:
    """A wiki with two points in K: z 分数 (procedure, interpret) and 标准差 (term)."""
    env = (MemoryRepo(["u1"]), Script(), Ids())
    page1(env)
    return env[0], ProfileService(env[0], MemoryProfileStore(), clock=lambda: T, new_id=env[2])


def _profile_obs(repo: MemoryRepo):
    return [e.observation for e in repo.aspect_log("u1") if isinstance(e, Observe)
            and e.observation.evidence_ref.startswith("profile:")]


def _typed(note: str, section: str, line: str) -> str:
    return note.replace(f"## {section}", f"## {section}\n- {line}")


def test_a_known_line_becomes_self_evidence_in_k_and_shows_back_from_k():
    from evowiki.goals.selfreport import SELF_CLAIM
    repo, svc = _k_service()
    out = _save(svc, _typed(svc.note("u1"), KNOWN, "z 分数：会怎么算"))
    obs = _profile_obs(repo)
    assert [(o.cell, o.kind, o.polarity, o.strength) for o in obs] == [
        ("c1.procedure", "self", *SELF_CLAIM["can_do"])]          # only the aspect he named
    assert obs[0].quote == "z 分数：会怎么算"
    assert "z 分数：会怎么算" in parse(out)[KNOWN]                  # K shows it back
    assert svc._store.get("u1").owned == {}                        # never stored in the profile
    _save(svc, out)                                             # saving again records nothing
    assert len(_profile_obs(repo)) == 1


def _bands(repo: MemoryRepo, *cells: str) -> list[str]:
    k = repo.load("u1").k
    return [k.belief(c, T).band.value for c in cells]


def test_deleting_a_known_line_takes_the_self_report_back_and_undo_says_it_again():
    """The failure: the line left the note, K kept the 「我会」 and everything downstream believed it."""
    from evowiki.belief.aspects import RetractSelf
    from evowiki.bot.executors_profile import KnownRetractExecutor
    from evowiki.store.bot import ActionRow
    repo, svc = _k_service()
    said = _save(svc, _typed(svc.note("u1"), KNOWN, "z 分数：会怎么算"))
    with_it = _bands(repo, "c1.procedure", "c1.interpret", "c2.term")
    assert with_it[0] != "none"

    log_rows: list = []
    class Log:
        def record_in(self, conn, user_id, key, retraction): log_rows.append((key, retraction))
        def action_id(self, key): return "act_1"
    svc.known_log = Log()
    gone = svc.save("u1", said.replace("- z 分数：会怎么算\n", ""), base=svc.version("u1"))
    assert "z 分数：会怎么算" not in gone.text
    assert gone.retracted.lines == ("z 分数：会怎么算",) and gone.retracted.action_id == "act_1"
    # nothing he said about it counts any more: direct weight and what it inferred, by an event
    k = repo.load("u1").k
    assert k.self_reports("c1.procedure") == () and k.belief("c1.procedure", T).direct == 0
    assert _bands(repo, "c1.procedure", "c1.interpret", "c2.term") == ["none", "none", "none"]
    assert any(isinstance(e, RetractSelf) for e in repo.aspect_log("u1"))
    assert repo.load("u1").k.notes("c1") == ()
    # a later save has nothing more to take back
    assert svc.save("u1", gone.text, base=svc.version("u1")).retracted is None

    key, r = log_rows[0]
    row = ActionRow(action_id="act_1", user_id="u1", kind="known_retract", scope="suggestion",
                    level="auto", status="executed", source="user", summary="s",
                    content={"lines": list(r.lines)}, content_hash="h", idempotency_key=key,
                    created_at=T, undo=r.undo)
    KnownRetractExecutor(repo, svc._store, lambda: T, svc._new_id).undo("u1", row)
    assert _bands(repo, "c1.procedure", "c1.interpret", "c2.term") == with_it
    assert "z 分数：会怎么算" in svc.note("u1")


def test_deleting_a_line_never_touches_what_his_answers_proved():
    from evowiki.belief.aspects import Observe
    from evowiki.goals.contracts import Observation
    from evowiki.wikiapp.repo import Commit
    repo, svc = _k_service()
    snap = repo.load("u1")
    right = [Observe(event_id=f"ok{i}", ts=T, observation=Observation(
        cell="c1.procedure", polarity="+", strength=1.0, kind="recall", evidence_ref=f"review:r{i}"))
        for i in range(3)]
    repo.commit(snap, Commit(aspect=right))
    proved = repo.load("u1").k.belief("c1.procedure", T)
    out = _save(svc, _typed(svc.note("u1"), KNOWN, "z 分数：会怎么算"))
    gone = svc.save("u1", out.replace("- z 分数：会怎么算\n", ""), base=svc.version("u1"))
    assert gone.retracted is not None
    after = repo.load("u1").k.belief("c1.procedure", T)
    assert after.band is proved.band and repo.load("u1").k.self_reports("c1.procedure") == () and after.direct > 0


def test_a_line_hidden_before_is_not_taken_back_by_a_later_save():
    """Found in real Obsidian: typing a new line retracted an older self-report nobody touched."""
    repo, svc = _k_service()
    _save(svc, _typed(svc.note("u1"), KNOWN, "z 分数：会怎么算"))
    before = repo.load("u1").k.self_reports("c1.procedure")
    assert before
    svc._store.update("u1", lambda p: p.model_copy(update={"dropped": {KNOWN: ("z 分数：会怎么算",)}}))
    note = svc.note("u1")
    assert "- z 分数：会怎么算\n" not in note
    out = svc.save("u1", note, base=svc.version("u1"))
    assert out.retracted is None and repo.load("u1").k.self_reports("c1.procedure") == before


def test_deleting_a_line_clears_the_know_he_declared():
    from evowiki.belief.aspects import Declare
    from evowiki.wikiapp.repo import Commit
    repo, svc = _k_service()
    snap = repo.load("u1")
    repo.commit(snap, Commit(aspect=[Declare(event_id="dk", ts=T, cell="c1.procedure", value="know")]))
    note = svc.note("u1")
    assert "z 分数：会怎么算" in note
    out = svc.save("u1", note.replace("- z 分数：会怎么算\n", ""), base=svc.version("u1"))
    assert out.retracted.lines == ("z 分数：会怎么算",)
    assert repo.load("u1").k.belief("c1.procedure", T).declared is None


@pytest.mark.parametrize("line,why", [
    ("我会微积分", "没找到这个知识点"),
    ("z 分数我会", "要写清会它的哪方面"),
    ("z 分数和标准差都会算", "一行只写一个知识点"),
])
def test_an_unmatched_known_line_is_not_recorded_and_says_why(line, why):
    repo, svc = _k_service()
    out = _save(svc, _typed(svc.note("u1"), KNOWN, line))
    assert _profile_obs(repo) == [] and svc._store.get("u1").owned == {}
    [back] = [x for x in parse(out)[KNOWN] if x.startswith(line)]
    assert f"（没记下：{why}" in back
    assert svc.note("u1") == out        # the plugin reads the note right after its own write
    # saved again as it came back: still not recorded, the reason not doubled
    assert parse(_save(svc, out))[KNOWN] == (back,) and _profile_obs(repo) == []
    # he deletes it: gone (nothing kept it)
    assert parse(_save(svc, out.replace(f"- {back}\n", "")))[KNOWN] == ()


def test_without_a_wiki_a_known_line_is_refused_visibly():
    svc = ProfileService(MemoryRepo([]), MemoryProfileStore(), clock=lambda: T, new_id=Ids())
    svc.note("u9")
    out = svc.edit("u9", f"## {KNOWN}\n- z 分数：会怎么算\n", base=svc.version("u9"))
    assert "z 分数：会怎么算（没记下：还没有知识点" in out and svc._store.get("u9").owned == {}


def test_the_profile_stores_only_who_and_want_lines():
    """Every typed line lands in its one store; anything outside the named sections is kept under
    我是谁. An old profile's other lines are not read (no migration)."""
    repo, svc = _k_service()
    text = (f"## {WHO}\n- 护士\n## {WANT}\n- 看懂 PCA\n## {HOW}\n- 先给例子\n## {WHERE}\n- 学了一半\n"
            f"## {KNOWN}\n- 标准差：会是什么\n## {GAPS}\n- 不会微积分\n## 其他\n- 周末有空\n")
    out = parse(_save(svc, text))
    p = svc._store.get("u1")
    assert p.owned == {WHO: ("护士", "学了一半", "不会微积分", "周末有空"), WANT: ("看懂 PCA",)}
    assert [r.text for r in said_rules(repo.load("u1").state)] == ["先给例子"]
    assert [o.cell for o in _profile_obs(repo)] == ["c2.term"]
    assert out[HOW] == ("先给例子",) and "标准差：会是什么" in out[KNOWN]
    assert "其他" not in out
    # an old profile's 怎么学比较好 / 已经会的 / own-heading lines are dropped on read
    old = Profile(owned={HOW: ("每天半小时",), KNOWN: ("会算均值",), "其他": ("x",)})
    assert shown(old, {})[HOW] == () and shown(old, {})[KNOWN] == () and "其他" not in shown(old, {})
    assert context_for(shown(old, {}), "goals.intake", owned=old.owned) == ""


@pytest.mark.parametrize("section", [HOW, KNOWN, WHERE, GAPS, "其他"])
def test_the_lander_lands_about_me_only_in_who_or_want(section):
    """Memory's lander (S3PreferenceLander) and the chat's cards land through `land_preference`: an
    about_me fact of any other section is refused before anything is written."""
    repo = MemoryRepo(["u1"])
    ev = EvidenceRef(session_id="s0", message_id="m9", role="user", quote="我每天只有半小时")
    got = land_preference(repo, repo.load("u1"), kind="about_me", text="每天只有半小时",
                          provenance="explicit_history", evidence=(ev,),
                          scope=MemoryScope(scope_type="global"), section=section,
                          now=T, new_id=Ids())
    assert got.status == "refused" and section in (got.reason or "")


def test_a_renamed_heading_revokes_no_rule_and_hides_nothing():
    _repo, svc = _k_service()
    note = _typed(svc.note("u1"), HOW, "数学题先解释符号")
    _save(svc, note)
    before = parse(svc.note("u1"))
    assert "数学题先解释符号" in before[HOW]
    after = parse(_save(svc, svc.note("u1").replace(f"## {HOW}", "## 学习方式")))
    assert "数学题先解释符号" in after[HOW]
    assert "数学题先解释符号" not in after[WHO]


# the old note's heading for his rules (before 2026-10-29); its section no longer exists
OLD_HEADING = "过去说过的要求"


def _rule_note(svc: ProfileService) -> tuple[str, str]:
    rule = "页面里多配几个能动手的交互组件"
    _save(svc, _typed(svc.note("u1"), HOW, rule))
    return rule, svc.note("u1")


def _moved(note: str, rule: str, to: str) -> str:
    """`note` with the rule's line taken out of 怎么学比较好 and put under the heading `to`."""
    return note.replace(f"- {rule}\n", "").replace(f"## {WHO}", f"## {to}\n- {rule}\n\n## {WHO}", 1)


def test_a_rule_line_under_another_heading_is_not_revoked():
    """Live 2026-11-03 22:47: a stale note with the rule under an old heading revoked it."""
    _repo, svc = _k_service()
    rule, note = _rule_note(svc)
    for to in (OLD_HEADING, WHO):
        after = parse(_save(svc, _moved(note, rule, to)))
        assert rule in after[HOW] and rule not in after[WHO]


def test_a_rule_line_deleted_from_the_note_is_still_revoked():
    _repo, svc = _k_service()
    rule, note = _rule_note(svc)
    after = parse(_save(svc, note.replace(f"- {rule}\n", "")))
    assert rule not in after[HOW] and rule not in after[WHO]


def test_an_empty_note_changes_nothing():
    _repo, svc = _k_service()
    rule, note = _rule_note(svc)
    _save(svc, _typed(note, WHO, "护士"))
    after = parse(_save(svc, ""))
    assert rule in after[HOW] and "护士" in after[WHO]


def test_a_restored_rule_takes_its_text_out_of_who():
    """A note with the revoked rule's text under 我是谁: once the rule is back it leaves 我是谁."""
    repo, svc = _k_service()
    rule, note = _rule_note(svc)
    stale = parse(_save(svc, note.replace(f"- {rule}\n", "")))
    assert rule not in stale[HOW]
    with_text = _typed(svc.note("u1"), WHO, rule)
    assert rule in parse(_save(svc, with_text))[WHO]
    add_guideline(repo, "u1", GuidelineIn(text=rule), now=T, new_id=svc._new_id)
    after = parse(_save(svc, with_text))
    assert rule in after[HOW] and rule not in after[WHO]


BARE = "".join(f"## {s}\n\n" for s in (WHO, WANT, HOW, WHERE, KNOWN, GAPS))


def _with_rule_and_fact(svc: ProfileService) -> tuple[str, str, str]:
    """A rule and a line of 我是谁, saved; (rule, fact, the note he now holds)."""
    rule, note = _rule_note(svc)
    return rule, "护士", _save(svc, _typed(note, WHO, "护士"))


@pytest.mark.parametrize("base", [None, "another-copy"])
def test_a_save_of_another_copy_retracts_nothing_and_keeps_both_sides(base):
    _repo, svc = _k_service()
    rule, fact, note = _with_rule_and_fact(svc)
    stale = _typed(note.replace(f"- {rule}\n", "").replace(f"- {fact}\n", ""), WHO, "夜班多")
    got = svc.save("u1", stale, base)
    after = parse(got.text)
    assert rule in after[HOW] and set(after[WHO]) >= {fact, "夜班多"}
    assert set(got.kept) == {rule, fact}


def test_a_save_of_the_last_copy_still_deletes_his_lines():
    _repo, svc = _k_service()
    rule, fact, note = _with_rule_and_fact(svc)
    got = svc.save("u1", note.replace(f"- {rule}\n", "").replace(f"- {fact}\n", ""), svc.version("u1"))
    after = parse(got.text)
    assert rule not in after[HOW] and fact not in after[WHO] and got.kept == ()


def test_a_copy_read_elsewhere_makes_the_older_copy_stale():
    """Live 2026-11-03: a GET moved the server's idea of his copy; the older note then revoked a rule."""
    _repo, svc = _k_service()
    rule, _fact, note = _with_rule_and_fact(svc)
    base = svc.version("u1")
    svc.add_fact("u1", WHO, "周末有空")
    svc.note("u1")                                       # another window reads the note
    after = parse(svc.edit("u1", note.replace(f"- {rule}\n", ""), base))
    assert rule in after[HOW] and "周末有空" in after[WHO]


def test_a_fact_written_after_his_copy_is_not_one_he_deleted():
    _repo, svc = _k_service()
    _rule, fact, note = _with_rule_and_fact(svc)
    svc.add_fact("u1", WHO, "周末有空")                    # from the chat; no new copy went out
    after = parse(svc.edit("u1", note, svc.version("u1")))
    assert set(after[WHO]) == {fact, "周末有空"}


@pytest.mark.parametrize("text", ["", BARE])
def test_a_note_without_a_line_changes_nothing_even_on_the_last_copy(text):
    _repo, svc = _k_service()
    rule, fact, _note = _with_rule_and_fact(svc)
    before = svc._store.get("u1")
    got = svc.save("u1", text, svc.version("u1"))
    assert (svc._store.get("u1").owned, svc._store.get("u1").dropped) == (before.owned, before.dropped)
    assert rule in parse(got.text)[HOW] and fact in parse(got.text)[WHO] and set(got.kept) == {rule, fact}


def test_a_note_of_another_copy_shows_again_what_was_hidden():
    """The state of me on 2026-11-04: owned {} and every derived line dropped; the vault's note brings them back."""
    _repo, svc = _k_service()
    note = svc.note("u1")
    derived = [x for s in (WHERE, GAPS) for x in parse(note).get(s, ())]
    assert derived
    svc._store.update("u1", lambda p: p.model_copy(update={"dropped": {GAPS: parse(note).get(GAPS, ())}, "owned": {}}))
    after = parse(svc.edit("u1", _typed(note, WHO, "护士")))
    assert after[GAPS] == parse(note)[GAPS] and after[WHO] == ("护士",)


def test_the_version_follows_the_text():
    _repo, svc = _k_service()
    a, b = svc.copy("u1"), svc.copy("u1")
    assert a.version == b.version and a.version
    svc.add_fact("u1", WHO, "护士")
    assert svc.copy("u1").version != a.version
