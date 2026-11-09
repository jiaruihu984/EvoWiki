"""The writer's EvoUI templates (examples.json, EvoUI v2 Task 8): each one is a {name, use, block} that
passes both validators (Python here; the JS one also in Chromium, and in `test_evoui_parity.py`) and
the block render check (headless Chromium, skipped saying why without it); the prompt text carries them
in the order that `max_examples` relies on (most broadly useful first, the tier-4 app last)."""

import pytest

from evowiki.deep.evoui import JS_PATH, TIERS_RULE, node_keys, spec_for_prompt, templates, validate
from evowiki.deep.evoui.contracts import EvoUIBlock
from evowiki.deep.render import PlaywrightChecker

NAMES = ["参数实验室", "诊断测验", "闪卡 → 测验", "步骤动画", "播放参数扫描", "多参数 + 大号结果", "计算器",
         "情景切换 + 时序图", "阶段链", "练习翻页", "限时练习", "选择学习方式", "面积图 + 悬停读数", "时间线", "深入一点",
         "小程序（第四层）"]


def kinds(block) -> set[str]:
    out: set[str] = set()

    def walk(n) -> None:
        if isinstance(n, dict):
            if "kind" in n:
                out.add(n["kind"])
            for c in n.get("children") or ():
                walk(c)
            walk(n.get("then"))
            walk(n.get("else"))

    walk(block["body"])
    return out


def _walk(n, kind):
    if isinstance(n, dict):
        if n.get("kind") == kind:
            yield n
        for c in n.get("children") or ():
            yield from _walk(c, kind)
        yield from _walk(n.get("then"), kind)
        yield from _walk(n.get("else"), kind)


def by_name() -> dict[str, dict]:
    return {t["name"]: t["block"] for t in templates()}


def test_every_template_is_name_use_block_and_valid():
    ts = templates()
    assert [t["name"] for t in ts] == NAMES
    for t in ts:
        assert set(t) == {"name", "use", "block", "group"} and t["use"].strip(), t["name"]
        assert validate(t["block"]) == [], (t["name"], validate(t["block"]))
        EvoUIBlock.model_validate(t["block"])
        assert t["block"]["question"].strip() and t["block"]["fallback"].strip(), t["name"]


def test_the_templates_cover_the_chatgpt_patterns():
    b = by_name()
    quiz = lambda n: next(x for x in node_keys(b[n]).values() if x["kind"] == "quiz")
    d = quiz("诊断测验")
    assert d["layout"] == "paged" and d["cases"]
    assert {q.get("type", "single") for q in d["questions"]} == {"single", "multi", "tf"}
    assert all("feedback" in q and "cell" in q for q in d["questions"]) and any("hint" in q for q in d["questions"])
    assert {"flashcards", "reveal", "quiz"} <= kinds(b["闪卡 → 测验"]) and quiz("闪卡 → 测验")["layout"] == "paged"
    assert {"player", "plot", "transition", "reveal", "celebrate", "grid"} <= kinds(b["步骤动画"])
    sort = b["步骤动画"]                       # two algorithms side by side, one player, four input presets
    assert sum(1 for _ in _walk(sort["body"], "plot")) == 2 and sum(1 for _ in _walk(sort["body"], "player")) == 1
    assert [o["value"] for o in next(_walk(sort["body"], "choice"))["options"]] == ["rand", "near", "up", "down"]
    timer_badge = next(_walk(b["限时练习"]["body"], "badge"))["text"]
    assert "时间到" in timer_badge and "超时" not in timer_badge   # a bound countdown stops at its seconds
    assert {"player", "slider", "plot", "quiz"} <= kinds(b["播放参数扫描"])
    assert {"pager", "fill-blank", "sentence"} <= kinds(b["练习翻页"])
    assert {"timer", "quiz"} <= kinds(b["限时练习"])
    route = next(x for x in node_keys(b["选择学习方式"]).values() if x["kind"] == "route")
    assert route["preference"] == "teaching_mode" and all(o["next"] for o in route["options"])
    chart = next(x for x in b["面积图 + 悬停读数"]["body"]["children"] if x["kind"] == "chart")
    assert chart["chart"] == "area" and chart["stack"] and chart["hover"] in b["面积图 + 悬停读数"]["state"]
    app = [n for n in node_keys(b["小程序（第四层）"]).values() if n["kind"] == "app"]
    assert len(app) == 1 and "evowiki.ready()" in app[0]["html"] and "evowiki.item(" in app[0]["html"]
    assert all("app" not in kinds(t["block"]) for t in templates()[:-1])     # tier 4 only in the last one


def test_spec_for_prompt_carries_the_templates_in_order_and_max_examples_keeps_the_first():
    full = spec_for_prompt()
    pos = [full.index(f"### {n}（") for n in NAMES]
    assert pos == sorted(pos)
    two = spec_for_prompt(2)
    assert "### 参数实验室（" in two and "### 诊断测验（" in two and "### 闪卡 → 测验（" not in two
    assert "小程序（第四层）" not in two.split("## 模板")[1]
    assert "四层" in TIERS_RULE and "app" in TIERS_RULE and "fallback" in TIERS_RULE
    assert "不要为它写 app" in full and "翻转的卡片堆" not in full     # flip cards are `flashcards`, not tier 4


@pytest.fixture(scope="module")
def checker():
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium not available ({type(exc).__name__}); "
                    "uv sync --extra deep && uv run playwright install chromium")
    return PlaywrightChecker()


@pytest.mark.parametrize("name", NAMES)
def test_every_template_passes_the_render_check(checker, name):
    r = checker.check_block("template", EvoUIBlock.model_validate(by_name()[name]))
    assert r.ok, r
    assert (r.network_requests, r.overflow_px, r.page_errors, r.console_errors) == (0, 0, (), ())
    assert r.probes_passed == r.probes_total


def test_every_template_passes_the_js_validator(checker):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        br = pw.chromium.launch(headless=True)
        page = br.new_page()
        page.set_content("<!doctype html><meta charset=utf-8><body></body>")
        page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
        got = page.evaluate("ts => ts.map(t => EvoUI.validate(t.block))", templates())
        br.close()
    assert got == [[] for _ in NAMES], got
