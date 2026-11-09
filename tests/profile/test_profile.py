"""The learner profile 「我」: his edits win, the server's lines follow its sources, every model call
of a listed purpose gets the right sections through the gateway, and a want never becomes an
experience (audit D10)."""

from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from evowiki.agent.tools import ToolContext
from evowiki.assistant.tools.actions import ActionDeps, RememberAboutMeTool
from evowiki.goals.path import LearningPath, PathPage
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef, ProviderResponse
from evowiki.llm.prices import PriceTable
from evowiki.llm.usage import Account
from evowiki.llm.usage_control import UsageControl
from evowiki.profile.context import RULE, context_for
from evowiki.profile.note import (
    GAPS,
    HOW,
    KNOWN,
    WANT,
    WHERE,
    WHO,
    Profile,
    add_fact,
    merge_edit,
    parse,
    render,
    shown,
    with_sent,
)
from evowiki.profile.service import ProfileService
from evowiki.profile.store import MemoryProfileStore
from evowiki.wikiapp.events import PathRecord, PathSaved, UsageAdded, UsageRecord, WikiEvent
from evowiki.wikiapp.guidelines import list_guidelines
from evowiki.wikiapp.repo import Commit, MemoryRepo

T = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)


# ---- the note: his edits win ----

def _save(svc: ProfileService, text: str) -> str:
    """A save by the client that holds the last copy."""
    if not svc.version("u1"):
        svc.note("u1")
    return svc.edit("u1", text, base=svc.version("u1"))


def test_his_line_is_kept_and_a_derived_line_he_deleted_stays_hidden():
    derived = {WHERE: ("学统计：0/2 页学完",), GAPS: ("均值：计算过程",)}
    p = with_sent(Profile(), derived, "v1")
    text = render(shown(p, derived)).replace("- 均值：计算过程\n", "") + "- 我其实会算均值\n"
    p = merge_edit(p, derived, text, "v1").profile
    assert p.owned == {WHO: ("我其实会算均值",)}       # typed under 还不会的 (a view): kept as his
    assert p.dropped == {GAPS: ("均值：计算过程",)}
    assert shown(p, derived)[GAPS] == ()                    # still derived, still hidden
    # a line derived after the copy he edited is not one he deleted
    later = {**derived, GAPS: ("均值：计算过程", "方差：术语")}
    assert shown(p, later)[GAPS] == ("方差：术语",)


def test_free_lines_and_his_own_headings_are_kept_under_who():
    p = merge_edit(Profile(), {}, "随便写的开头\n\n## 我是谁\n* 护士\n1. 夜班多\n\n## 其他\n- 周末有空\n"
                   ).profile
    assert p.owned == {WHO: ("随便写的开头", "护士", "夜班多", "周末有空")}
    assert list(shown(p, {}))[-1] == GAPS


def test_a_fact_from_the_chat_updates_its_line():
    p = add_fact(Profile(), WHO, "线性代数完全零基础")
    p = add_fact(p, WHO, "线性代数学过一点", replaces="线性代数完全零基础")
    p = add_fact(p, WHO, "线性代数学过一点")
    assert p.owned[WHO] == ("线性代数学过一点",)
    with pytest.raises(ValueError):
        add_fact(p, WHERE, "x")


def test_parse_skips_frontmatter_and_bullets():
    assert parse("---\na: 1\n---\n## 想学什么\n- [ ] PCA\n") == {WANT: ("PCA",)}


def test_translated_generated_lines_keep_their_sources_when_a_stale_copy_is_saved():
    from evowiki.profile.note import note_version, render_note

    canonical = {WHERE: ("PCA：还没开始",), HOW: ("Use examples first",)}
    p = with_sent(Profile(), canonical, "old")
    english = render_note(p, canonical, language="en",
                          display={WHERE: {canonical[WHERE][0]: "PCA: not started"}})
    p = with_sent(p, canonical, note_version(english))
    edited = merge_edit(p, canonical, english, p.version)
    assert edited.profile.owned == {} and not edited.added and not edited.removed
    # A language refresh makes the earlier copy stale. Its translated progress is still
    # a derived line, and cannot silently become a fact or a teaching rule.
    refreshed = render_note(p, canonical, language="zh", display={})
    current = with_sent(p, canonical, note_version(refreshed))
    stale = merge_edit(current, {}, english + "\n## About me\n- I am a nurse\n", "old")
    assert stale.profile.owned == {WHO: ("I am a nurse",)}
    assert not stale.added and not stale.removed


def test_editing_a_translated_progress_line_preserves_the_users_actual_words():
    from evowiki.profile.note import render_note

    d = {WHERE: ("PCA：还没开始",)}
    p = with_sent(Profile(), d, "v")
    text = render_note(p, d, language="en", display={WHERE: {d[WHERE][0]: "PCA: not started"}})
    changed = text.replace("PCA: not started", "I studied PCA at university")
    out = merge_edit(p, d, changed, "v")
    assert out.profile.owned == {WHO: ("I studied PCA at university",)}


def test_moving_an_unchanged_translated_status_does_not_assert_a_rule_or_self_report():
    from evowiki.profile.note import render_note

    d = {WHERE: ("PCA：还没开始",)}
    p = with_sent(Profile(), d, "v")
    text = render_note(p, d, language="en", display={WHERE: {d[WHERE][0]: "PCA: not started"}})
    for heading in ("How I learn best", "What I know", "About me"):
        moved = text.replace("## My progress", f"## {heading}")
        out = merge_edit(p, d, moved, "v")
        assert not out.added and not out.profile.owned


def test_profile_language_changes_only_the_projection():
    from evowiki.wikiapp.events import LanguageSet

    repo, ids = _path_repo(), _Ids()
    svc = _service(repo)
    before = svc.copy("u1")
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id=ids("lang"), ts=T,
                                                       body=LanguageSet(language="en"))]))
    english = svc.copy("u1")
    assert english.locale == "en" and "## About me" in english.text
    assert "0 of 2 pages completed" in english.text
    assert "学 PCA" in english.text  # user content keeps its original language
    svc.save("u1", before.text, before.version)
    assert svc._store.get("u1").owned == {}


# ---- context_for: the one place that picks sections ----

SECTIONS = {WHO: ("护士",), WANT: ("给照片按尺寸重命名",), HOW: ("先给例子",),
            WHERE: ("学统计：1/2 页学完",), KNOWN: ("均值：会怎么算",), GAPS: ("方差：术语",)}


@pytest.mark.parametrize("purpose,has,lacks", [
    # 怎么学比较好 (his rules) reaches the writers and the assistant through the spec, not here
    # (T6); 已经会的 is K's view, no call gets it from the profile
    ("assistant.turn", (WHO, WANT, WHERE, GAPS), (HOW, KNOWN)),
    ("deep.write", (WHO, GAPS), (WANT, WHERE, HOW, KNOWN)),
    ("goals.path", (WANT, HOW, WHERE), (KNOWN,)),
    ("review.card_rewrite", (GAPS, HOW), (WHO, KNOWN)),
])
def test_each_purpose_gets_its_sections(purpose, has, lacks):
    block = context_for(SECTIONS, purpose)
    assert RULE in block
    for s in has:
        assert f"## {s}" in block, s
    for s in lacks:
        assert f"## {s}" not in block, s


def test_unlisted_purposes_and_an_empty_profile_get_nothing():
    assert context_for(SECTIONS, "graph.group_naming") == ""     # graph group naming
    assert context_for(SECTIONS, "goals.intakeX") == ""
    assert context_for({}, "assistant.turn") == ""
    # stage 4: the outline is the writer's (deep.write keeps the PAGE sections); no organize row
    assert context_for(SECTIONS, "goals.organize") == ""
    # add_practice is a practice run of the writer (deep.write's sections); no practice row
    assert context_for(SECTIONS, "wiki.practice") == ""


def test_the_budget_cuts_a_section_from_its_end():
    many = {WHO: tuple(f"第{i}件很长很长很长很长很长很长的事" for i in range(200))}
    block = context_for(many, "review.card_rewrite")
    assert block == "" or "第199件" not in block
    assert "第0件" in context_for(many, "assistant.turn") and "第199件" not in context_for(many, "assistant.turn")


def test_his_own_lines_win_the_budget_over_derived_ones():
    goals = tuple(f"第{i}个目标很长很长很长很长很长的目标名字" for i in range(60))
    sections = {WANT: (*goals, "我自己想学的矩阵乘法"), HOW: ("先给例子再讲公式",)}
    owned = {WANT: ("我自己想学的矩阵乘法",)}
    block = context_for(sections, "goals.reading.unit", owned=owned)
    assert "我自己想学的矩阵乘法" in block
    assert "第0个" in block and "第59个" not in block      # derived lines fill what is left
    assert "## 怎么学比较好" not in block           # a later section's derived lines go first
    assert block.index("第0个") < block.index("我自己想学的矩阵乘法")     # the note's order inside
    # without `owned` the derived lines starve his: what the fix is for
    assert "我自己想学的矩阵乘法" not in context_for(sections, "goals.reading.unit")


# ---- the service over the wiki, and the gateway ----

def _path_repo() -> MemoryRepo:
    repo = MemoryRepo(["u1"])
    pages = tuple(PathPage(page_id=f"pp{i}", targets=(f"c{i}",), concepts=(f"c{i}",),
                           objective=f"学完这页你能用 c{i}", new_cells=4) for i in (1, 2))
    path = LearningPath(path_id="path-k", goal="学 PCA", goal_key="k", split=True, source="llm",
                        capacity=12, new_cells=8, concepts=(), pages=pages)
    rec = PathRecord(path=path, page_ids=(None, None), created_at=T)
    repo.commit(repo.load("u1"), Commit(wiki=[WikiEvent(event_id="wev_p", ts=T, body=PathSaved(
        record=rec, action="planned"))]))
    return repo


class _Ids:
    n = 0

    def __call__(self, prefix: str) -> str:
        self.n += 1
        return f"{prefix}_{self.n}"


def _service(repo=None) -> ProfileService:
    return ProfileService(repo or _path_repo(), MemoryProfileStore(), clock=lambda: T, new_id=_Ids())


def test_the_note_carries_goals_and_progress():
    sec = parse(_service().note("u1"))
    assert sec[WANT] == ("学 PCA",)
    # what GET /wiki/progress says: the next step is the path's first row (no page written yet)
    assert sec[WHERE] == ("学 PCA：2 页里学完 0 页，下一步：开始「学完这页你能用 c1」",)
    assert sec[GAPS] == ()


def test_the_snapshot_is_loaded_again_only_when_the_wiki_moved():
    repo = _path_repo()
    loads = []
    load = repo.load
    repo.load = lambda uid: loads.append(uid) or load(uid)     # type: ignore[method-assign]
    svc = _service(repo)
    svc.add_fact("u1", WHO, "护士")
    first = svc.context("u1", "assistant.turn")
    assert svc.context("u1", "goals.path") and svc.context("u1", "assistant.turn") == first
    assert loads == ["u1"]
    svc.add_fact("u1", WANT, "看懂论文里的 PCA")      # his profile is read fresh each time
    assert "看懂论文里的 PCA" in svc.context("u1", "assistant.turn") and loads == ["u1"]
    repo.commit(load("u1"), Commit(wiki=[WikiEvent(event_id="wev_q", ts=T, body=UsageAdded(
        record=UsageRecord(action="拒绝原则", text="x")))]))
    svc.context("u1", "assistant.turn")
    assert loads == ["u1", "u1"]


def test_a_how_line_is_a_rule_and_deleting_it_revokes_the_rule():
    """怎么学比较好 is a view of his rules: a line he types adds a rule (origin user_ui), a line
    he deletes revokes it (RuleDecided revoked); the profile stores neither."""
    from evowiki.rules.decisions import Decisions
    from evowiki.wikiapp.guidelines import said_rules
    repo = _path_repo()
    svc = _service(repo)
    note = svc.note("u1")
    _save(svc, note.replace(f"## {HOW}", f"## {HOW}\n- 先给例子再讲公式"))
    [rule] = said_rules(repo.load("u1").state)
    assert (rule.text, rule.origin, rule.authority) == ("先给例子再讲公式", "user_ui", "user")
    assert "- 先给例子再讲公式" in svc.note("u1")
    assert svc._store.get("u1").owned == {}                      # the spec is its one store
    _save(svc, svc.note("u1").replace("- 先给例子再讲公式\n", ""))
    state = repo.load("u1").state
    assert list_guidelines(state).items == [] and said_rules(state) == ()
    assert [(d.rule_id, d.action) for d in Decisions.of(state).active()] == [
        (rule.item_id, "revoked")]
    assert "先给例子再讲公式" not in svc.note("u1")


class _Answer(BaseModel):
    ok: bool


class _Recorder:
    def __init__(self):
        self.systems: list[str] = []

    def complete(self, model, request, json_schema):
        self.systems.append(request.system)
        return ProviderResponse(raw_output='{"ok": true}', model_version=model, cost=None)


class _Meter:
    def check(self, account):
        pass

    def record(self, account, purpose, usage, *, replayed):
        pass


def _gateway(tmp_path, svc, rec):
    ref = ModelRef(provider="gen", model="g")
    return Gateway(GatewayConfig(generator=ref, verifier=ref, allow_single_provider=True),
                   CallLog(tmp_path / "calls.jsonl"), mode="record", providers={"gen": rec},
                   clock=lambda: T, usage=UsageControl(_Meter(), PriceTable({})),
                   profile=svc.context)


ACC = Account(user_id="u1", group="page")


def _call(gw, purpose, account=ACC):
    req = LLMRequest(purpose=purpose, prompt_version="v1", subject=purpose, system="静态规则",
                     prompt="p")
    gw.call("generator", req, _Answer, account=account)


def _remember(svc: ProfileService) -> RememberAboutMeTool:
    """remember_about_me over an in-memory ledger whose profile_fact executor writes `svc`'s store
    (stage 3 T1: a fact is a ledger row, undoable)."""
    from types import SimpleNamespace

    from evowiki.bot.executors_profile import KIND, ProfileFactExecutor
    from tests.assistant.memory_ledger import MemoryLedger
    ledger = MemoryLedger({KIND: ProfileFactExecutor(svc._store)})
    return RememberAboutMeTool(ActionDeps(repo=SimpleNamespace(load=lambda uid: None),  # type: ignore[arg-type]
                                          ledger=ledger, clock=lambda: T,  # type: ignore[arg-type]
                                          new_id=_Ids()))


def test_said_in_chat_then_edited_reaches_the_next_intake_and_page_write(tmp_path):
    """零基础 said in the chat → 「我」 shows it → he edits the note → the next intake / page write
    gets the edited text, at the end of its system prompt."""
    svc, rec = _service(), _Recorder()
    gw = _gateway(tmp_path, svc, rec)
    tool = _remember(svc)
    said = "我线性代数完全零基础，每天只有半小时"
    ctx = ToolContext(user_id="u1", session_id="s1", user_words=(said,), current_words=(said,),
                      message_id="m1")
    assert tool.run({"applies": "lasting", "section": WHO, "quote": "线性代数完全零基础", "text": "线性代数零基础"},
                    ctx).error is None
    note = svc.note("u1")
    assert "- 线性代数零基础" in note
    _save(svc, note.replace("- 线性代数零基础", "- 线性代数学过一点，忘得差不多了"))
    _call(gw, "goals.intake")
    _call(gw, "deep.write")
    _call(gw, "graph.group_naming")
    _call(gw, "goals.intake", account=None)
    intake, write, naming, anonymous = rec.systems
    for s in (intake, write):
        assert s.startswith("静态规则\n\n") and "线性代数学过一点，忘得差不多了" in s
        assert "线性代数零基础" not in s
    assert naming == "静态规则" and anonymous == "静态规则"


@pytest.mark.parametrize("section", [HOW, KNOWN])
def test_remember_about_me_refuses_how_and_known(section):
    """How to teach him is a rule (remember_preference), what he knows is K (record_evidence):
    remember_about_me writes neither, and no path stores a line there."""
    svc = _service()
    tool = _remember(svc)
    said = "我每天只有半小时，均值我会算"
    ctx = ToolContext(user_id="u1", session_id="s1", user_words=(said,), current_words=(said,),
                      message_id="m1")
    res = tool.run({"applies": "lasting", "section": section, "quote": "每天只有半小时",
                    "text": "每天能学半小时"}, ctx)
    assert res.error is not None and svc._store.get("u1") == Profile()
    with pytest.raises(ValueError):
        add_fact(Profile(), section, "x")


def test_d10_a_want_is_never_written_as_experience(tmp_path):
    """Audit D10: he asked about renaming photos, then switched to PCA. The writer must not be told
    he is already familiar with sized photo names: the want sits under 想学什么 (which the writer
    is not sent), nothing he did not say reaches 我是谁, and the block says a want is not
    an experience."""
    svc, rec = _service(), _Recorder()
    gw = _gateway(tmp_path, svc, rec)
    tool = _remember(svc)
    said = ("我想写个脚本把照片按尺寸重命名", "算了，照片先不弄了，我想先学 PCA")
    # both lines of this turn (his message and his reply to its question): quotes come from them
    ctx = ToolContext(user_id="u1", session_id="s1", user_words=said, current_words=said,
                      message_id="m1")
    assert tool.run({"applies": "lasting", "section": WANT, "quote": "把照片按尺寸重命名",
                         "text": "写脚本把照片按尺寸重命名"}, ctx).error is None
    # an experience he never stated cannot be quoted, so it is refused
    assert tool.run({"applies": "lasting", "section": WHO, "quote": "我很熟带尺寸的照片文件名",
                     "text": "熟悉带尺寸的照片文件名"}, ctx).error is not None
    _call(gw, "deep.write")
    _call(gw, "assistant.turn")
    write, chat = rec.systems
    assert "照片" not in write and RULE in chat
    sec = {s.split("\n", 1)[0]: s for s in chat.split("## ")[1:]}
    assert "照片" in sec[WANT] and "照片" not in sec.get(WHO, "")
