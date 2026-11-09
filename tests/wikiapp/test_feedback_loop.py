"""B1: page feedback understood and answered at once (scripted gateway, MemoryRepo, no network)."""

import pytest

from evowiki.deep.contracts import LibraryComponent, Probe, RenderCheck
from evowiki.deep.store import MemoryAssetStore
from evowiki.teach.contracts import PageFeedbackIn
from evowiki.wikiapp.feedback_loop import page_feedback_loop, undo_feedback
from evowiki.wikiapp.llm import CountingGateway
from evowiki.wikiapp.page import set_fold
from evowiki.wikiapp.repo import WikiConflict
from evowiki.wikiapp.understand import PURPOSE, REPAIR_TURNS, restates
from evowiki.wikiapp.views import feedback_out
from tests.wikiapp.fake_writer import WRITE
from tests.wikiapp.partial_write_check import assert_partial_write
from tests.wikiapp.test_goal import NOW
from tests.wikiapp.test_page import organized
from tests.wikiapp.test_teach_generation import env, st  # noqa: F401 — fixture

TEXT = "标准差那段我完全看不懂，能不能画个图让我动手看看"
LAB = LibraryComponent(
    component_id="variance-dots", name="方差点图", description="拖动分散程度，看偏差的平方怎样变",
    learning_question="分散程度变大时，方差怎么变？", params={"分散程度": "0–2.5"},
    html="<!doctype html><button id=b>拖</button><div id=r>1</div>", height=420,
    probes=(Probe(target="#b", action="click", expect_change="#r"),),
    check=RenderCheck(interactive_id="variance-dots", ok=True, probes_passed=1, probes_total=1))


def answer(**kw) -> dict:
    base = {"reading": "你看不懂标准差那段，想要一个能动手看的图",
            "missing": [],
            "page": {"summary": "给标准差那段补一个直观说法，再放一个能拖的图",
                     "sections": [{"section_id": "s2", "add": "先别管公式：标准差就是“典型的偏离有多大”。"}],
                     "component": "variance-dots", "component_after": "s2"},
            "style": {"kind": "缺少可视化", "before": "交互只在确实需要时才做",
                      "after": "有数值和分布的概念都配一个能动手的图"}}
    return {**base, **kw}


def run_fb(env, pid, body=None, components=(LAB,), assets="new"):  # noqa: F811
    repo, s, ids = env
    store = MemoryAssetStore() if assets == "new" else assets
    gw = CountingGateway(s.gateway(), "u1")
    out = page_feedback_loop(repo, gw, "u1", pid, body or PageFeedbackIn(text=TEXT, chips=("unclear",)),
                             now=NOW, new_id=ids, components=components, assets=store)
    return out, store


def test_understood_feedback_patches_the_page_and_places_the_component(env):  # noqa: F811
    pid = organized(env)
    env[1].push(PURPOSE, answer())
    before = st(env).pages[pid]
    out, store = run_fb(env, pid)
    assert out.understood and out.reading.reading.startswith("你看不懂标准差")
    assert out.rsi == "queued" and out.llm_calls == 1 and out.feedback_id
    assert [x.section_id for x in out.sections] == ["s2"] and out.component.component_id == "variance-dots"
    rec = st(env).pages[pid]
    assert rec.page.texts["s2"].body.endswith("标准差就是“典型的偏离有多大”。")
    sid = out.component.section_id
    order = [s.id for s in rec.page.outline.sections]
    assert order.index(sid) == order.index("s2") + 1
    assert "embedded-html" in rec.page.texts[sid].body and rec.changed[sid] == "inserted"
    [asset] = store.for_page("u1", pid)
    assert asset.interactive_id == "variance-dots" and asset.asset_id == out.component.asset_id
    mark = feedback_out(rec)
    assert mark.can_undo and mark.summary.startswith("给标准差") and set(mark.sections) == {"s2", sid}
    fb = st(env).feedbacks[out.feedback_id]
    assert fb.before == before and fb.after == rec
    assert_partial_write(env[0], pid, "feedback", {"s2", sid}, producer="writer")
    assert rec.written.job_id == f"feedback:{out.feedback_id}"
    req = env[1].requests(PURPOSE)[0].prompt
    assert TEXT in req and "variance-dots" in req and "[s2]" in req


def test_undo_restores_the_page_once_and_only_while_unchanged(env):  # noqa: F811
    pid = organized(env)
    env[1].push(PURPOSE, answer(), answer())
    before = st(env).pages[pid].page
    out, _ = run_fb(env, pid)
    undo_feedback(env[0], "u1", pid, out.feedback_id, now=NOW, new_id=env[2])
    rec = st(env).pages[pid]
    assert rec.page.outline == before.outline and rec.page.texts == before.texts
    assert feedback_out(rec) is None
    with pytest.raises(WikiConflict):
        undo_feedback(env[0], "u1", pid, out.feedback_id, now=NOW, new_id=env[2])
    with pytest.raises(LookupError):
        undo_feedback(env[0], "u1", pid, "nope", now=NOW, new_id=env[2])
    out2, _ = run_fb(env, pid)
    set_fold(env[0], "u1", pid, "s2", True, now=NOW, new_id=env[2])     # display only: still undoable
    undo_feedback(env[0], "u1", pid, out2.feedback_id, now=NOW, new_id=env[2])


def test_undo_refuses_after_the_page_content_changed(env):  # noqa: F811
    from evowiki.wikiapp.events import PageSaved, WikiEvent
    from evowiki.wikiapp.repo import Commit
    pid = organized(env)
    env[1].push(PURPOSE, answer())
    out, _ = run_fb(env, pid)
    repo, _, ids = env
    snap = repo.load("u1")
    rec = snap.state.pages[pid]
    t = rec.page.texts["s3"]
    page = rec.page.model_copy(update={"texts": {**rec.page.texts,
                                                 "s3": t.model_copy(update={"body": "别的改动"})}})
    repo.commit(snap, Commit(wiki=[WikiEvent(event_id=ids("wev"), ts=NOW, body=PageSaved(
        record=rec.model_copy(update={"page": page}), action="patched"))]))
    with pytest.raises(WikiConflict):
        undo_feedback(repo, "u1", pid, out.feedback_id, now=NOW, new_id=ids)


def test_missing_concepts_become_dont_and_the_page_reacts(env):  # noqa: F811
    pid = organized(env)
    env[1].push(PURPOSE, answer(missing=["c2"], page={"summary": "无", "sections": []}))
    out, _ = run_fb(env, pid, components=())
    assert out.understood and [m.concept_id for m in out.missing] == ["c2"]
    assert out.missing[0].cells == ["c2.term"]
    cell = env[0].load("u1").kview(NOW).cell("c2.term")
    assert cell.declared == "dont" and cell.band.value == "gap"
    assert out.feedback_id is not None                          # the K pass is undoable as a unit


def test_unknown_component_section_and_concept_are_dropped_not_invented(env):  # noqa: F811
    pid = organized(env)
    env[1].push(PURPOSE, answer(missing=["量子力学"],
                                page={"summary": "改", "component": "made-up",
                                      "sections": [{"section_id": "s9", "add": "x"},
                                                   {"section_id": "s2", "add": "补一句"}]}))
    out, store = run_fb(env, pid)
    assert out.understood and out.component is None and store.for_page("u1", pid) == []
    assert set(out.dropped) == {"概念 量子力学", "小节 s9", "组件 made-up"}
    assert [x.section_id for x in out.sections] == ["s2"] and out.missing == []


def test_a_reading_that_ignores_the_words_fails_closed(env):  # noqa: F811
    pid = organized(env)
    env[1].push(PURPOSE, answer(reading="用户希望页面更好"))
    before = st(env).pages[pid]
    out, _ = run_fb(env, pid)
    assert not out.understood and "请切换模型重试" in out.reason and out.rsi == "not_understood"
    assert st(env).pages[pid] == before
    assert st(env).signals_recorded == 1 and out.signal_id          # the signal is still kept


def test_broken_answer_or_no_gateway_only_records_the_signal(env):  # noqa: F811
    pid = organized(env)
    env[1].push(PURPOSE, *["not json"] * (1 + REPAIR_TURNS))   # asked again within the repair turns
    before = st(env).pages[pid]
    out, _ = run_fb(env, pid)
    assert not out.understood and "请切换模型重试" in out.reason and st(env).pages[pid] == before
    repo, _, ids = env
    out2 = page_feedback_loop(repo, None, "u1", pid, PageFeedbackIn(text=TEXT), now=NOW,
                              new_id=ids)
    assert not out2.understood and "请切换模型重试" in out2.reason
    assert st(env).signals_recorded == 2


def test_feelings_only_or_apply_off_do_not_queue_rsi(env):  # noqa: F811
    pid = organized(env)
    env[1].push(PURPOSE, answer(reading="你觉得太浅了"), answer())
    out, _ = run_fb(env, pid, PageFeedbackIn(chips=("too_shallow",)))
    assert out.understood and out.rsi == "no_text"
    out, _ = run_fb(env, pid, PageFeedbackIn(text=TEXT, apply=False))
    assert out.rsi == "not_requested"


def test_without_an_asset_store_no_component_is_offered(env):  # noqa: F811
    pid = organized(env)
    env[1].push(PURPOSE, answer())
    out, _ = run_fb(env, pid, assets=None)
    assert out.component is None and "组件 variance-dots" in out.dropped
    assert "组件库是空的" in env[1].requests(PURPOSE)[0].prompt


def test_restates():
    assert restates("你看不懂标准差那段", "标准差那段我看不懂")
    assert not restates("用户希望页面更好", "标准差那段我看不懂")
    assert restates("任何话", "")                                # feelings only
    assert WRITE                                                # writer untouched by this flow


def _only_sections(prompt, *keys):
    from evowiki.teach.standard import SECTION_KEYS, marker
    for k in SECTION_KEYS:
        assert (marker(k) in prompt) == (k in keys), k


def test_understanding_prompt_injects_checklist_writing_rules_and_routes(env):  # noqa: F811
    from evowiki.wikiapp.llm import JSON_PROMPT_VERSION
    pid = organized(env)
    env[1].push(PURPOSE, answer())
    run_fb(env, pid)
    req = env[1].requests(PURPOSE)[0]
    _only_sections(req.prompt, "self_check", "terms_first", "tables_after", "order", "spoken_first",
                   "feedback_split", "visual_question", "interactive_tiers")
    assert "coherence. 讲解连贯" in req.prompt                    # the checklist ids (standard@v14)
    assert "这一页" in req.prompt and "K：" in req.prompt and "讲法" in req.prompt   # three routes
    assert "拆细，不是加长" in req.prompt and "服务于具体的学习问题" in req.prompt
    assert "不能编造库里没有的组件" in req.prompt          # component part of visual_question
    assert "时间、等待、先后顺序" not in req.prompt
    assert "原样接在那一节正文后面" in req.prompt
    assert "（coherence）" in req.prompt and "直接回应他的原话" in req.prompt
    assert "交互按四层选" in req.prompt and "沙盒小程序" in req.prompt     # TIERS_RULE (EvoUI v2)
    assert req.prompt_version == JSON_PROMPT_VERSION[PURPOSE] == "feedback-understand@v14"


# ---- configurable components (component-library) ----

def _configurable():
    from tests.deep.test_components import configurable
    return configurable()


def _answer_with(config, why="让他一步步看复制怎么进行"):
    """The reading picks the component (its config is null there); `config` is pushed as the answer of
    the config call that follows (feedback-understand@v12, wikiapp.component_fill)."""
    a = answer()
    a["page"] = {**a["page"], "component": "steps-demo", "component_config": None, "component_why": why}
    return a


def _push_with(env, config, configs: int = 1, why="让他一步步看复制怎么进行") -> None:  # noqa: F811
    env[1].push(PURPOSE, _answer_with(config, why), *[{"config": config}] * configs)


def _run_cfg(env, pid, checker):  # noqa: F811
    repo, s, ids = env
    store = MemoryAssetStore()
    out = page_feedback_loop(repo, CountingGateway(s.gateway(), "u1"), "u1", pid,
                             PageFeedbackIn(text=TEXT), now=NOW, new_id=ids,
                             components=(_configurable(),), assets=store, checker=checker)
    return out, store


def test_a_configurable_component_is_placed_with_its_config_after_a_check(env):  # noqa: F811
    from evowiki.deep.contracts import CONFIG_ELEMENT_ID
    from tests.deep.fakes import FakeChecker
    from tests.deep.test_components import CFG
    pid = organized(env)
    _push_with(env, CFG)
    checker = FakeChecker()
    out, store = _run_cfg(env, pid, checker)
    assert out.component.component_id == "steps-demo" and out.component.name == "复制 DNA"
    assert out.component.why == "让他一步步看复制怎么进行" and checker.checked == ["steps-demo"]
    [asset] = store.for_page("u1", pid)
    html = store.get("u1", asset.asset_id).html
    assert CONFIG_ELEMENT_ID in html and "复制 DNA" in html
    assert out.reading.page.component_config == CFG
    rec = st(env).pages[pid]
    assert rec.page.texts[out.component.section_id].body.startswith("每一步变了什么？")
    prompt = env[1].requests(PURPOSE)[0].prompt
    assert "[可配置]" in prompt and "string?" not in prompt and "component_config" in prompt
    # the config is asked for on its own, after the reading, with the component named
    second = env[1].requests(PURPOSE)[1].prompt
    assert "steps-demo" in second and "只填它的 config" in second


def test_v12_a_config_written_as_a_json_string_does_not_parse(env):  # noqa: F811
    """feedback-understand@v12: the config call is typed by the component's own schema, so a JSON
    string is no answer: after the repair turns the component is dropped; the text still lands."""
    import json

    from tests.deep.fakes import FakeChecker
    from tests.deep.test_components import CFG
    pid = organized(env)
    _push_with(env, json.dumps(CFG, ensure_ascii=False), configs=1 + REPAIR_TURNS)
    out, store = _run_cfg(env, pid, FakeChecker())
    assert out.understood and out.component is None and store.for_page("u1", pid) == []


@pytest.mark.parametrize("config, checker, why", [
    ({"title": "复制 DNA"}, "ok", "config 不合法"),
    ("ok", None, "没有渲染检查"),
    ("ok", "fail", "没通过渲染检查"),
])
def test_a_configurable_component_fails_closed(env, config, checker, why):  # noqa: F811
    from tests.deep.fakes import FakeChecker
    from tests.deep.test_components import CFG
    pid = organized(env)
    # a config its schema refuses goes back for the repair turns first, then is dropped
    _push_with(env, CFG if config == "ok" else config, configs=1 if config == "ok" else 1 + REPAIR_TURNS)
    chk = {"ok": FakeChecker(), "fail": FakeChecker(lambda d: "按钮没反应"), None: None}[checker]
    out, store = _run_cfg(env, pid, chk)
    assert out.understood and out.component is None and store.for_page("u1", pid) == []
    assert any(why in d for d in out.dropped), out.dropped
    assert [x.section_id for x in out.sections] == ["s2"]          # the text still lands


def _block_answer(block, component=None) -> dict:
    a = answer()
    a["page"] = {**a["page"], "component": component, "component_after": "s2",
                 "block": block, "block_why": "他看不出偏差怎样随分散变化"}
    return a


def _run_block(env, pid, checker, components=(LAB,), assets="new"):  # noqa: F811
    repo, s, ids = env
    store = MemoryAssetStore() if assets == "new" else assets
    out = page_feedback_loop(repo, CountingGateway(s.gateway(), "u1"), "u1", pid,
                             PageFeedbackIn(text=TEXT), now=NOW, new_id=ids,
                             components=components, assets=store, checker=checker)
    return out, store


def test_an_evoui_block_is_placed_as_a_fence_after_its_check(env):  # noqa: F811
    import json

    from tests.deep.fakes import FakeChecker
    from tests.deep.test_evoui_typed_templates import EXAMPLES
    BASE = EXAMPLES["lab"]
    pid = organized(env)
    env[1].push(PURPOSE, _block_answer(BASE))
    checker = FakeChecker()
    out, store = _run_block(env, pid, checker)
    assert out.component.kind == "block" and out.component.component_id == "evoui-block"
    assert out.component.why == "他看不出偏差怎样随分散变化" and checker.checked == ["evoui-block"]
    [asset] = store.for_page("u1", pid)
    assert asset.kind == "evoui"
    assert json.loads(store.get("u1", asset.asset_id).html)["title"] == BASE["title"]
    body = st(env).pages[pid].page.texts[out.component.section_id].body
    assert f"```evoui\nasset: {asset.asset_id}\n```" in body and "embedded-html" not in body
    prompt = env[1].requests(PURPOSE)[0].prompt
    assert "EvoUI" in prompt and "block_why" in prompt


@pytest.mark.parametrize("case, why", [
    ("invalid", "不合法"), ("no-checker", "没有渲染检查"), ("fail", "没通过渲染检查"),
    ("both", "只用组件"), ("no-store", "不能存交互"),
])
def test_an_evoui_block_fails_closed(env, case, why):  # noqa: F811
    from tests.deep.fakes import FakeChecker
    from tests.deep.test_evoui_typed_templates import EXAMPLES
    BASE = EXAMPLES["lab"]
    pid = organized(env)
    block = {**BASE, "derived": [{"name": "x", "expr": "nope + 1"}]} if case == "invalid" else BASE
    # an invalid block goes back for the repair turns first, then is dropped
    env[1].push(PURPOSE, *[_block_answer(block, "variance-dots" if case == "both" else None)] * (1 + REPAIR_TURNS))
    chk = {"fail": FakeChecker(fail_block=lambda bid, b: "空白"), "no-checker": None}.get(
        case, FakeChecker())
    out, store = _run_block(env, pid, chk, assets=None if case == "no-store" else "new")
    assert out.understood and any(why in d for d in out.dropped), out.dropped
    if case == "both":                                    # the component still lands
        assert out.component.kind == "component" and out.component.component_id == "variance-dots"
    else:
        assert out.component is None
        assert store is None or store.for_page("u1", pid) == []
    assert [x.section_id for x in out.sections] == ["s2"]
