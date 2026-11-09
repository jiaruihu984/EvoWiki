"""EvoUI v2 Task 7: the `app` node (a sandboxed HTML mini-app) in headless Chromium (skipped, saying why,
without it). The iframe's sandbox attribute and the srcdoc layout (CSP meta, tokens, bridge first in
<head>), the bridge (accepted messages become D3 events and progress; wrong channel / source / item / shape /
size / rate are dropped and counted), the frame cannot reach the parent, progress and theme reach the app,
the fallback, the render check on good and bad apps, and an app's item events graded into K end to end."""

import copy
import re
from pathlib import Path

import pytest

from evowiki.deep.evoui import CSS_PATH, JS_PATH, node_keys, quiz_cells, validate
from evowiki.deep.evoui.contracts import APP_CSP, APP_SANDBOX, MAX_APP_EVENTS, EvoUIBlock
from evowiki.deep.render import PlaywrightChecker, cdn_allowed
from evowiki.wikiapp.wiki_schemas import BlockEventIn, BlockResultsIn

APPS = Path(__file__).parent / "apps"
FLIPDECK = (APPS / "flipdeck.html").read_text(encoding="utf-8")
FOURIER = (APPS / "fourier.html").read_text(encoding="utf-8")
DECK_IDS = ["d-sin", "d-cos", "d-exp", "d-ln", "d-pow"]

# a tiny app the tests drive through `frame.evaluate`: it shows what init brought
PROBE = """<p id=out>start</p><button id=b onclick="this.textContent = 'pressed'">press</button>
<script>
evowiki.onInit(function (p) { document.getElementById('out').textContent = 'init:' + JSON.stringify(p) + ':' + evowiki.theme; });
evowiki.ready();
</script>"""


def block(html=PROBE, items=None, **kw) -> dict:
    node = {"kind": "app", "id": "lab", "html": html,
            "items": items if items is not None else [{"id": "a", "label": "第一项", "cell": "c1.interpret"},
                                                       {"id": "b", "label": "第二项"}], **kw}
    return {"type": "evoui", "title": "小程序", "question": "动手看看", "state": {}, "derived": {},
            "body": {"kind": "box", "children": [{"kind": "text", "text": "说明"}, node]},
            "fallback": "这是小程序的文字版。"}


SETUP = """([b, p]) => {
  window.got = {events: [], warn: [], progress: []};
  const w = console.warn; console.warn = (...a) => { window.got.warn.push(a.join(' ')); w(...a); };
  const opts = {blockId: 'blk', onEvent: e => got.events.push(e), onProgress: x => got.progress.push(x)};
  if (p) opts.progress = p;
  window.h = EvoUI.render(b, document.getElementById('m'), opts);
}"""


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed (uv sync --extra deep)")
    with sync_playwright() as pw:
        try:
            br = pw.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"chromium not available ({type(exc).__name__}); uv run playwright install chromium")
        yield br
        br.close()


def mount(browser, b=None, progress=None, scheme="light", ready=True, width=640):
    page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme=scheme)
    page.set_content("<!doctype html><meta charset=utf-8><body><div id=m></div></body>")
    page.add_style_tag(content=CSS_PATH.read_text(encoding="utf-8"))
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate(SETUP, [b or block(), progress])
    if ready:
        page.wait_for_function("() => document.querySelector('.evoui-app')?.dataset.ready === 'true'", timeout=8000)
    return page


def frame_of(page):
    return page.locator(".evoui-app iframe").first.element_handle().content_frame()


def channel(page) -> str:
    return re.search(r'"(evoui-[0-9a-f]{32})"', page.locator(".evoui-app iframe").get_attribute("srcdoc")).group(1)


def got(page, what="events"):
    return page.evaluate(f"() => window.got.{what}")


def rejected(page) -> int:
    return page.evaluate("() => h.rejected()")


def settle(page, ms=150):
    page.wait_for_timeout(ms)


# ---- the frame ----

def test_the_frame_is_sandboxed_and_the_csp_tokens_and_bridge_come_first(browser):
    page = mount(browser)
    f = page.locator(".evoui-app iframe")
    attrs = f.evaluate("e => Object.fromEntries([...e.attributes].map(a => [a.name, a.value]))")
    assert attrs["sandbox"] == APP_SANDBOX == "allow-scripts"
    assert attrs["referrerpolicy"] == "no-referrer"
    assert set(attrs) == {"class", "sandbox", "referrerpolicy", "title", "style", "srcdoc"}      # no allow=
    assert attrs["srcdoc"].startswith('<!doctype html><html data-theme="light"><head><meta http-equiv='
                                      f'"Content-Security-Policy" content="{APP_CSP}"><style id="evowiki-tokens">')
    head = frame_of(page).evaluate("""() => [...document.head.children].slice(0, 3).map(e =>
        [e.tagName, e.getAttribute('http-equiv'), e.getAttribute('content'), e.id])""")
    assert head == [["META", "Content-Security-Policy", APP_CSP, ""], ["STYLE", None, None, "evowiki-tokens"],
                    ["SCRIPT", None, None, ""]]
    page.close()


def test_an_apps_own_doctype_html_and_head_merge_after_the_injected_head(browser):
    tricky = ("<!-- <head><meta name=x> --><!doctype html><html lang=\"zh-CN\" data-x=\"1\"><head><title>T</title>"
              "<style>p{color:red}</style></head><body><p id=out>hi</p><script>evowiki.ready()</script></body></html>")
    page = mount(browser, block(tricky))
    fr = frame_of(page)
    got_ = fr.evaluate("""() => ({first: document.head.firstElementChild.getAttribute('http-equiv'),
        lang: document.documentElement.lang, x: document.documentElement.dataset.x, theme: document.documentElement.dataset.theme,
        title: document.title, heads: document.querySelectorAll('head').length, text: document.body.innerText.trim()})""")
    assert got_ == {"first": "Content-Security-Policy", "lang": "zh-CN", "x": "1", "theme": "light", "title": "T",
                    "heads": 1, "text": "hi"}
    page.close()


def test_the_frame_cannot_reach_the_parent_or_the_network(browser):
    """The static scan is evaded on purpose (names built from pieces): the sandbox and the CSP still hold."""
    evil = """<pre id=r></pre><script>
    var EVIL = "https:" + "//evil.example/", out = [], W = window, P = W["par" + "ent"], T = W["to" + "p"];
    function t(name, fn) { try { out.push(name + ":ok:" + String(fn())); } catch (e) { out.push(name + ":blocked:" + e.name); } }
    t("p-document", function () { return P.document.title; });
    t("p-body", function () { P.document.body.innerHTML = "PWNED"; return 1; });
    t("p-write", function () { P.__pwned = 1; return P.__pwned; });
    t("t-read", function () { return T.location.href; });
    t("t-navigate", function () { T["loca" + "tion"].href = EVIL; return 1; });
    t("storage", function () { return W["local" + "Storage"].length; });
    t("cookie", function () { return document["coo" + "kie"]; });
    t("frameElement", function () { return W.frameElement; });
    t("open", function () { return W.open(EVIL); });
    t("form", function () { var f = document.createElement("form"); f.action = EVIL; document.body.appendChild(f); f.submit(); return 1; });
    fetch(EVIL + "x").then(function () { out.push("fetch:ok"); }, function (e) { out.push("fetch:blocked:" + e.name); })
      .then(function () { document.getElementById("r").textContent = out.join("\\n"); evowiki.ready(); });
    </script>"""
    assert validate(block(evil)) == []                      # the scan does not see it ...
    reqs = []
    page = browser.new_page()
    page.on("request", lambda r: reqs.append(r.url))
    page.set_content("<!doctype html><meta charset=utf-8><title>host</title><body><div id=m></div></body>")
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    url = page.url
    page.evaluate(SETUP, [block(evil), None])
    page.wait_for_function("() => document.querySelector('.evoui-app')?.dataset.ready === 'true'", timeout=8000)
    res = dict(line.split(":", 1) for line in frame_of(page).locator("#r").inner_text().splitlines())
    assert all(res[k].startswith("blocked:") for k in ("p-document", "p-body", "p-write",
                                                        "t-read", "t-navigate", "storage", "cookie",
                                                        "fetch")), res
    assert res["frameElement"] == "ok:null" and res["open"] == "ok:null"        # no handle on the frame, no popup
    assert page.title() == "host" and page.url == url and "PWNED" not in page.evaluate("() => document.body.innerText")
    assert page.locator("#m .evoui .evoui-app iframe").count() == 1
    assert page.evaluate("() => window.__pwned") is None
    assert not [u for u in reqs if "evil.example" in u]     # ... the CSP and the sandbox stop everything
    assert len(page.context.pages) == 1
    page.close()


# ---- the bridge ----

def test_accepted_messages_become_events_progress_and_frame_height(browser):
    page = mount(browser)
    fr = frame_of(page)
    assert page.locator(".evoui-app").get_attribute("data-state") == "ready"
    assert not page.locator(".evoui-app-fallback").is_visible()
    fr.evaluate("() => evowiki.item('a', {x: 1, y: [1, 2]}, true)")
    fr.evaluate("() => evowiki.item('b', 'free', null, true, 1234)")
    fr.evaluate("() => evowiki.summary({total: 2, right: 1, missed: ['b']})")
    fr.evaluate("() => evowiki.item('a', 3, false)")
    fr.evaluate("() => evowiki.save({step: 4, seen: ['a']})")
    settle(page)
    ev = got(page)
    for e in ev:
        BlockEventIn.model_validate(e)                      # exactly the backend's D3 shape
    items = [(e["type"], e.get("item"), e.get("attempt"), e.get("response"), e.get("correct"), e.get("hintUsed"))
             for e in ev]
    assert items == [("item", "a", 1, {"x": 1, "y": [1, 2]}, True, False), ("item", "b", 1, "free", None, True),
                     ("summary", None, 1, None, None, None), ("item", "a", 2, 3, False, False)]
    assert all(e["node"] == "lab" and e["kind"] == "app" and e["block"] == "blk" for e in ev)
    assert isinstance(ev[0]["latencyMs"], int) and ev[0]["latencyMs"] >= 0 and ev[1]["latencyMs"] == 1234
    s = ev[2]
    assert (s["total"], s["right"], s["missed"], s["hintsUsed"]) == (2, 1, ["b"], 1) and s["timeMs"] >= 0
    revs = [e["revision"] for e in ev]
    assert revs == sorted(set(revs))
    prog = page.evaluate("() => h.progress()")
    assert prog["nodes"]["lab"] == {"round": 2, "saved": {"step": 4, "seen": ["a"]}}
    for want, h in ((200, 100), (1200, 5000), (515, 514.6)):
        fr.evaluate(f"() => evowiki.resize({h})")
        settle(page, 80)
        assert page.locator(".evoui-app iframe").evaluate("e => e.style.height") == f"{want}px"
    assert rejected(page) == 0 and got(page, "warn") == []
    page.close()


def test_progress_comes_back_to_the_app_and_nothing_fires_again(browser):
    page = mount(browser)
    fr = frame_of(page)
    fr.evaluate("() => { evowiki.item('a', 1, true); evowiki.save({n: 7}); }")
    settle(page)
    prog = page.evaluate("() => h.flush()")
    page.close()
    page = mount(browser, progress=prog)
    page.wait_for_function("() => true")
    fr = frame_of(page)
    fr.wait_for_function("() => document.getElementById('out').textContent.startsWith('init:')")
    assert fr.locator("#out").inner_text() == 'init:{"n":7}:light'
    assert fr.evaluate("() => evowiki.progress") == {"n": 7}
    assert got(page) == [] and got(page, "progress") == []
    fr.evaluate("() => evowiki.item('a', 2, true)")
    settle(page)
    assert [e["attempt"] for e in got(page)] == [1]          # restored round 1 (no summary yet)
    page.close()
    # a progress that does not fit the node is discarded for it, with a warning
    page = mount(browser, progress={**prog, "nodes": {"lab": {"round": 0, "saved": None}}})
    assert any("does not fit" in w for w in got(page, "warn"))
    assert frame_of(page).evaluate("() => evowiki.progress") is None
    page.close()


def test_bad_messages_are_dropped_counted_and_warned(browser):
    page = mount(browser)
    fr, ch = frame_of(page), channel(page)
    bad = [
        {"channel": "evoui-" + "0" * 32, "type": "ready"},                    # wrong channel
        {"type": "ready"},                                                     # no channel
        {"channel": ch, "type": "item", "id": "zzz", "response": 1},           # undeclared item
        {"channel": ch, "type": "item", "id": "a", "response": "x" * 2100},    # over 2000 characters
        {"channel": ch, "type": "item", "id": "a", "correct": "yes"},
        {"channel": ch, "type": "item", "id": "a", "hintUsed": 1},
        {"channel": ch, "type": "item", "id": "a", "latencyMs": -5},
        {"channel": ch, "type": "item", "id": "a", "latencyMs": 1.5},
        {"channel": ch, "type": "item", "id": "a", "extra": 1},                # unknown field
        {"channel": ch, "type": "hack"},                                       # unknown type
        {"channel": ch, "type": "summary", "total": 2, "right": 3},
        {"channel": ch, "type": "summary", "total": 2, "right": 1, "missed": ["zzz"]},
        {"channel": ch, "type": "save", "progress": {"x": "y" * 16001}},       # save over 16000
        {"channel": ch, "type": "save"},
        {"channel": ch, "type": "resize", "height": "big"},
    ]
    fr.evaluate("ms => ms.forEach(m => window['par' + 'ent'].postMessage(m, '*'))", bad)
    fr.evaluate("() => { window['par' + 'ent'].postMessage('just text', '*'); "
                "window['par' + 'ent'].postMessage({channel: '" + ch + "', type: 'item', id: 'a', response: new Date()}, '*'); }")
    settle(page, 250)
    assert rejected(page) == len(bad) + 2
    assert got(page) == []
    warns = got(page, "warn")
    assert len(warns) == len(bad) + 2 and all(w.startswith("EvoUI app lab: dropped a message") for w in warns)
    assert any("wrong channel" in w for w in warns) and any("not one of the app's items" in w for w in warns)
    assert any("larger than 2000" in w for w in warns) and any("larger than 16000" in w for w in warns)
    # the right channel from another window: the host page itself, and a second frame
    page.evaluate("ch => window.postMessage({channel: ch, type: 'item', id: 'a', response: 1}, '*')", ch)
    page.evaluate("() => { const f = document.createElement('iframe'); f.id = 'other'; document.body.append(f); }")
    other = page.locator("#other").element_handle().content_frame()
    other.evaluate("ch => parent.postMessage({channel: ch, type: 'item', id: 'a', response: 1}, '*')", ch)
    settle(page, 200)
    assert rejected(page) == len(bad) + 4 and got(page) == []
    assert sum("not from this app's frame" in w for w in got(page, "warn")) == 2
    # someone else's message (another channel, another window) is not ours: ignored silently
    page.evaluate("() => window.postMessage({channel: 'evoui-other', type: 'ready'}, '*')")
    settle(page)
    assert rejected(page) == len(bad) + 4
    page.close()


def test_more_than_500_accepted_messages_per_render_are_dropped(browser):
    page = mount(browser)                                       # ready was accepted message 1
    fr = frame_of(page)
    fr.evaluate(f"() => {{ for (let i = 0; i < {MAX_APP_EVENTS - 2}; i++) evowiki.save({{i}}); }}")
    fr.evaluate("() => evowiki.item('a', 1, true)")             # message 500: accepted
    fr.evaluate("() => { evowiki.item('a', 2, true); evowiki.save({x: 1}); }")
    settle(page, 400)
    assert [e["response"] for e in got(page)] == [1]
    assert rejected(page) == 2
    assert any("more than 500 messages" in w for w in got(page, "warn"))
    page.close()


def test_the_fallback_shows_under_the_frame_when_the_app_never_says_ready(browser):
    page = mount(browser, block("<p>never ready</p>"), ready=False)
    assert not page.locator(".evoui-app-fallback").is_visible()
    page.wait_for_function("() => document.querySelector('.evoui-app')?.dataset.state === 'fallback'", timeout=9500)
    assert page.locator(".evoui-app-fallback").is_visible()
    assert "这是小程序的文字版。" in page.locator(".evoui-app-fallback").inner_text()
    assert page.locator(".evoui-app iframe").count() == 1          # the frame stays
    page.close()
    # where frames cannot run (no srcdoc): the fallback at once, no frame
    page = browser.new_page()
    page.set_content("<!doctype html><meta charset=utf-8><body><div id=m></div></body>")
    page.add_style_tag(content=CSS_PATH.read_text(encoding="utf-8"))
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate("() => { delete HTMLIFrameElement.prototype.srcdoc; }")
    page.evaluate(SETUP, [block(), None])
    assert page.locator(".evoui-app").get_attribute("data-fallback") == "unsupported"
    assert page.locator(".evoui-app-fallback").is_visible() and page.locator(".evoui-app iframe").count() == 0
    page.close()


def test_the_app_sees_the_host_tokens_and_follows_a_theme_change(browser):
    page = mount(browser, scheme="dark")
    fr = frame_of(page)
    host = page.evaluate("""() => { const s = document.createElement('span'); document.querySelector('.evoui-app').append(s);
        const out = {}; for (const [k, v] of [['--viz-1', '--eu-blue'], ['--text', '--eu-fg'], ['--known', '--eu-right']]) {
          s.style.color = 'var(' + v + ')'; out[k] = getComputedStyle(s).color; } s.remove(); return out; }""")
    inner = fr.evaluate("""() => { const cs = getComputedStyle(document.documentElement);
        return {theme: document.documentElement.dataset.theme, scheme: cs.colorScheme, viz: cs.getPropertyValue('--viz-1').trim(),
                text: cs.getPropertyValue('--text').trim(), known: cs.getPropertyValue('--known').trim(),
                alias: cs.getPropertyValue('--viz-text').trim(), api: evowiki.theme}; }""")
    assert inner["theme"] == "dark" and inner["scheme"] == "dark" and inner["api"] == "dark"
    assert (inner["viz"], inner["text"], inner["known"]) == (host["--viz-1"], host["--text"], host["--known"])
    assert inner["alias"] == host["--text"]                 # --viz-text: the ChatGPT-style alias
    assert page.locator(".evoui-app iframe").evaluate("e => e.style.colorScheme") == "dark"
    page.emulate_media(color_scheme="light")
    fr.wait_for_function("() => document.documentElement.dataset.theme === 'light' && evowiki.theme === 'light'")
    assert fr.evaluate("() => getComputedStyle(document.documentElement).getPropertyValue('--text').trim()") \
        == "rgb(34, 34, 34)"
    page.close()


# ---- the render check ----

def deck_block() -> dict:
    return {"type": "evoui", "title": "导数翻卡", "question": "常见函数的导数记住了吗？", "state": {}, "derived": {},
            "body": {"kind": "app", "id": "deck", "height": 400, "html": FLIPDECK,
                     "items": [{"id": i, "label": f"{i} 的导数", "cell": "c1.interpret"} for i in DECK_IDS]},
            "fallback": "sin→cos，cos→−sin，eˣ→eˣ，ln x→1/x，xⁿ→n·xⁿ⁻¹。"}


def fourier_block() -> dict:
    return {"type": "evoui", "title": "方波的傅里叶级数", "question": "为什么方波跳变处总有约 9% 的过冲？", "state": {},
            "derived": {}, "body": {"kind": "box", "children": [
                {"kind": "text", "text": "拖动 N，看部分和怎么逼近方波。", "tone": "secondary"},
                {"kind": "app", "height": 470, "html": FOURIER, "items": [{"id": "gibbs", "label": "吉布斯现象"}]}]},
            "fallback": "方波可以写成奇次正弦之和；项越多越接近，但跳变处的过冲始终约 9%（吉布斯现象）。"}


@pytest.fixture(scope="module")
def checker(browser):          # the browser fixture skips without Chromium
    return PlaywrightChecker()


def test_the_render_check_passes_the_fixture_apps(checker):
    for b in (deck_block(), fourier_block()):
        assert validate(b) == []
        r = checker.check_block("app", EvoUIBlock.model_validate(b))
        assert r.ok, r
        assert (r.console_errors, r.page_errors, r.network_requests, r.overflow_px) == ((), (), 0, 0)


BAD_APPS = {
    "fetch": (("<button id=go onclick=\"fetch('https://evil.example/data.json').catch(function () {});"
               " this.textContent = 'sent'\">载入</button><script>evowiki.ready()</script>"), "网络请求"),
    "overflow": (("<div style='width:2000px'>宽</div><button onclick=\"this.textContent = 'x'\">b</button>"
                  "<script>evowiki.ready()</script>"), "小程序里横向溢出"),
    "static": ("<p>不会变</p><button>没用</button><script>evowiki.ready()</script>", "画面都没有变化"),
    "throws": (("<button onclick=\"this.textContent = 'x'\">b</button><script>evowiki.ready();"
                " throw new Error('boom in the app')</script>"), None),
    "undeclared": (("<button onclick=\"evowiki.item('nope', 1, true); this.textContent = 'x'\">b</button>"
                    "<script>evowiki.ready()</script>"), "条不合规的消息"),
}


@pytest.mark.parametrize("name", list(BAD_APPS))
def test_the_render_check_fails_a_bad_app_and_says_why(checker, name):
    html, why = BAD_APPS[name]
    b = block(html)
    assert validate(b) == []
    r = checker.check_block(name, EvoUIBlock.model_validate(b))
    assert not r.ok
    if name == "fetch":
        assert r.network_requests >= 1 and "evil.example" in r.detail
    if name == "overflow":
        assert r.overflow_px > 1000
    if name == "throws":
        assert any("boom in the app" in e for e in r.page_errors), r
    else:
        assert why in r.detail, r.detail


def test_the_render_check_fails_an_app_that_never_says_ready(checker):
    r = checker.check_block("silent", EvoUIBlock.model_validate(block("<button onclick=\"this.textContent='x'\">b</button>")))
    assert not r.ok and "没有在 8 秒内调用 evowiki.ready()" in r.detail


def test_cdn_allowlist_in_the_render_check():
    assert cdn_allowed("https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js")
    assert cdn_allowed("https://unpkg.com/d3@7") and cdn_allowed("https://cdnjs.cloudflare.com/ajax/libs/x.js")
    assert not cdn_allowed("https://cdn.jsdelivr.net.evil.example/x.js")
    assert not cdn_allowed("http://cdn.jsdelivr.net/x.js") and not cdn_allowed("https://esm.sh/react")


# ---- end to end: app item events → POST results → K ----

def test_a_fixture_apps_items_are_graded_into_k(browser):
    from evowiki.belief.aspects import Observe
    from evowiki.wikiapp.block_progress import MemoryProgressStore
    from evowiki.wikiapp.block_results import block_results
    from evowiki.wikiapp.repo import MemoryRepo
    from tests.wikiapp.script import Script
    from tests.wikiapp.test_block_results import stored
    from tests.wikiapp.test_goal import NOW, Ids
    from tests.wikiapp.test_page import organized

    b = deck_block()
    assert validate(b) == [] and list(node_keys(b)) == ["deck"] and quiz_cells(b) == {"c1.interpret"}
    page = mount(browser, copy.deepcopy(b), width=420)
    fr = frame_of(page)
    for know in (True, False, True, True, False):
        fr.click("#card")
        fr.wait_for_function("() => !document.getElementById('know').disabled")
        fr.click("#know" if know else "#miss")
        page.wait_for_timeout(320)
    page.wait_for_function("() => window.got.events.some(e => e.type === 'summary')")
    events = got(page)
    page.close()
    assert [(e["type"], e.get("item"), e.get("correct")) for e in events] == [
        ("item", "d-sin", True), ("item", "d-cos", False), ("item", "d-exp", True), ("item", "d-ln", True),
        ("item", "d-pow", False), ("summary", None, None)]
    assert events[-1]["missed"] == ["d-cos", "d-pow"] and (events[-1]["total"], events[-1]["right"]) == (5, 3)

    env = (MemoryRepo(["u1"]), Script(), Ids())
    pid = organized(env)
    store, aid = stored(pid, block=b)
    out = block_results(env[0], store, None, "u1", aid, BlockResultsIn(events=events), now=NOW, new_id=env[2],
                        progress=MemoryProgressStore())
    assert [(o.status, o.correct, o.graded) for o in out.events] == [
        ("k", True, "client"), ("k", False, "client"), ("k", True, "client"), ("k", True, "client"),
        ("k", False, "client"), ("signal", None, None)]
    assert (out.right, out.total) == (3, 5)
    obs = {e.observation.evidence_ref: e.observation for e in env[0].aspect_log("u1") if isinstance(e, Observe)}
    got_ = [(obs[o.answer_ref].polarity, obs[o.answer_ref].strength, obs[o.answer_ref].cell) for o in out.events[:5]]
    assert got_ == [("+", 0.6, "c1.interpret"), ("-", 0.6, "c1.interpret"), ("+", 0.6, "c1.interpret"),
                    ("+", 0.6, "c1.interpret"), ("-", 0.6, "c1.interpret")]


def test_the_spec_example_is_a_valid_app_that_passes_the_render_check(checker):
    import json

    from evowiki.deep.evoui import spec_text
    m = re.search(r'```json\n(\{"kind": "app".*?)\n```', spec_text(), re.DOTALL)
    b = {"type": "evoui", "title": "转箭头", "question": "90° 在哪里？", "state": {}, "derived": {},
         "body": json.loads(m.group(1)), "fallback": "从竖直方向转 90° 就是水平方向。"}
    assert validate(b) == []
    r = checker.check_block("spec-example", EvoUIBlock.model_validate(b))
    assert r.ok, r


# ---- fix round 1 ----

# saves, then navigates itself away once (only when it had no progress yet); after a reload it shows what came back
NAVIGATOR = """<p id=out>start</p><button onclick="this.textContent = 'x'">b</button><script>
evowiki.onInit(function (p) {
  if (p) { document.getElementById('out').textContent = 'restored:' + p.n; evowiki.ready(); return; }
  evowiki.save({n: 1}); evowiki.ready();
  setTimeout(function () { window['loca' + 'tion']['hr' + 'ef'] = 'about:blank#away'; }, 300);
});
</script>"""


def test_a_frame_that_navigates_itself_is_removed_and_can_be_reloaded_fresh(browser):
    b = block(NAVIGATOR)
    assert validate(b) == []                          # the scan does not see the pieced-together navigation
    page = mount(browser, b, ready=False)
    first = channel(page)
    page.wait_for_function("() => document.querySelector('.evoui-app')?.dataset.fallback === 'navigated'", timeout=8000)
    assert page.locator(".evoui-app iframe").count() == 0
    assert page.locator(".evoui-app-fallback").is_visible() and page.locator(".evoui-app-reload").is_visible()
    assert page.locator(".evoui-app").get_attribute("data-ready") == "false"
    assert rejected(page) == 1 and any("navigated away" in w for w in got(page, "warn"))
    assert page.evaluate("() => h.progress()")["nodes"]["lab"]["saved"] == {"n": 1}
    page.locator(".evoui-app-reload").click()
    page.wait_for_function("() => document.querySelector('.evoui-app')?.dataset.ready === 'true'", timeout=8000)
    assert channel(page) != first                      # a new channel
    assert frame_of(page).locator("#out").inner_text() == "restored:1"
    assert not page.locator(".evoui-app-fallback").is_visible()
    assert page.locator(".evoui-app iframe").count() == 1 and rejected(page) == 1
    page.close()


def test_item_reports_are_capped_per_item_and_identical_repeats_skipped(browser):
    page = mount(browser)
    fr = frame_of(page)
    fr.evaluate("() => { evowiki.item('a', 1, true); evowiki.item('a', 1, true); evowiki.item('a', 1, true); }")
    fr.evaluate("() => { evowiki.item('a', 2, false); evowiki.item('a', 3, true); evowiki.item('a', 4, true); }")
    fr.evaluate("() => { evowiki.item('b', 1, true, false, 3600001); evowiki.item('b', 2, true, false, 3600000); }")
    settle(page, 250)
    ev = got(page)
    assert [(e["item"], e["response"]) for e in ev] == [("a", 1), ("a", 2), ("a", 3), ("b", 1), ("b", 2)]
    assert rejected(page) == 1 and any("already reported 3 times" in w for w in got(page, "warn"))
    lat = [e["latencyMs"] for e in ev if e["item"] == "b"]
    assert lat[0] < 3_600_000 and lat[1] == 3_600_000        # too big an own latency is replaced by the measured one
    for e in ev:
        BlockEventIn.model_validate(e)
    page.close()


def test_the_theme_listeners_stop_once_the_node_is_gone(browser):
    page = browser.new_page()
    page.set_content("<!doctype html><meta charset=utf-8><body><div id=m></div></body>")
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate("""() => { window.offs = {mo: 0, mq: 0};
        const d = MutationObserver.prototype.disconnect; MutationObserver.prototype.disconnect = function () { offs.mo += 1; return d.call(this); };
        const r = MediaQueryList.prototype.removeEventListener; MediaQueryList.prototype.removeEventListener = function (...a) { offs.mq += 1; return r.apply(this, a); }; }""")
    page.evaluate(SETUP, [block(), None])
    page.wait_for_function("() => document.querySelector('.evoui-app')?.dataset.ready === 'true'", timeout=8000)
    assert page.evaluate("() => offs") == {"mo": 0, "mq": 0}
    page.evaluate("() => document.getElementById('m').replaceChildren()")
    page.wait_for_function("() => offs.mo >= 1 && offs.mq >= 1", timeout=5000)
    page.close()


# ---- fix round 2 ----

def test_any_item_left_open_over_an_hour_reports_the_server_bound(browser):
    """ctx.emit clamps every item's latencyMs to BlockEventIn's bound (1 h), not only the app's."""
    quiz = {"type": "evoui", "title": "测验", "question": "q", "state": {}, "derived": {},
            "body": {"kind": "quiz", "questions": [{"q": "1 + 1？", "options": ["2", "3"], "correct": 0, "why": "w"}]},
            "fallback": "f"}
    page = browser.new_page()
    page.clock.install()
    page.set_content("<!doctype html><meta charset=utf-8><body><div id=m></div></body>")
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate(SETUP, [quiz, None])
    page.clock.fast_forward(3_700_000)
    page.locator("#m input[type=radio]").first.check()
    page.locator("#m .evoui-check").click()
    ev = [e for e in got(page) if e["type"] == "item"]
    assert len(ev) == 1 and ev[0]["latencyMs"] == 3_600_000
    for e in got(page):
        BlockEventIn.model_validate(e)
    page.close()


# ---- final review (M2, M3) ----

def test_a_transition_ghost_never_boots_a_second_copy_of_its_app(browser):
    """The leaving copy of a transition's content holds an empty box where the app frame was: a cloned
    srcdoc frame would run the app again on the same channel (rejected messages, a failed render check)."""
    b = block()
    app = b["body"]["children"].pop()
    b["state"] = {"k": 1}
    b["body"]["children"].append({"kind": "transition", "key": "k", "children": [app]})
    assert validate(b) == []
    page = mount(browser, b)
    settle(page, 400)                                           # the frame has followed its content
    h0 = page.locator(".evoui-app iframe").bounding_box()["height"]
    page.evaluate("() => h.set('k', 2)")
    assert page.locator("iframe").count() == 1                  # the ghost holds no frame ...
    ph = page.locator(".evoui-leaving .evoui-ghost-frame")
    assert ph.count() == 1 and abs(ph.bounding_box()["height"] - h0) < 1     # ... but a box of its size
    settle(page, 600)
    assert rejected(page) == 0 and not any("dropped" in w for w in got(page, "warn"))
    assert page.locator(".evoui-leaving").count() == 0 and page.locator("iframe").count() == 1
    page.close()


def test_a_detached_then_reattached_app_offers_a_reload(browser):
    """Detached long enough for the node to stop its bridge, then put back: the frame reloads with nobody
    listening, so it is removed and the fallback offers 「重新载入」, which starts a fresh, working frame."""
    page = mount(browser)
    fr = frame_of(page)
    fr.evaluate("() => evowiki.save({n: 7})")
    settle(page)
    first = channel(page)
    page.evaluate("() => { window.kept = document.getElementById('m').firstChild; kept.remove(); }")
    settle(page, 2300)                                       # the 2 s watch stops the bridge
    page.evaluate("() => document.getElementById('m').append(kept)")
    page.wait_for_function("() => document.querySelector('.evoui-app')?.dataset.fallback === 'detached'", timeout=8000)
    assert page.locator(".evoui-app iframe").count() == 0
    assert page.locator(".evoui-app-reload").is_visible() and rejected(page) == 0
    page.locator(".evoui-app-reload").click()
    page.wait_for_function("() => document.querySelector('.evoui-app')?.dataset.ready === 'true'", timeout=8000)
    assert channel(page) != first
    assert frame_of(page).locator("#out").inner_text().startswith('init:{"n":7}')
    frame_of(page).evaluate("() => evowiki.item('a', 1, true)")
    settle(page)
    assert [e["item"] for e in got(page)] == ["a"] and rejected(page) == 0
    page.close()


def test_destroy_stops_the_app_bridge_and_watchers_at_once(browser):
    page = browser.new_page()
    page.set_content("<!doctype html><meta charset=utf-8><body><div id=m></div></body>")
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate("""() => { window.offs = {mo: 0, mq: 0};
        const d = MutationObserver.prototype.disconnect; MutationObserver.prototype.disconnect = function () { offs.mo += 1; return d.call(this); };
        const r = MediaQueryList.prototype.removeEventListener; MediaQueryList.prototype.removeEventListener = function (...a) { offs.mq += 1; return r.apply(this, a); }; }""")
    page.evaluate(SETUP, [block(), None])
    page.wait_for_function("() => document.querySelector('.evoui-app')?.dataset.ready === 'true'", timeout=8000)
    fr = frame_of(page)
    fr.evaluate("() => evowiki.save({n: 3})")
    settle(page, 50)
    n_progress = len(got(page, "progress"))
    out = page.evaluate("() => h.destroy()")                       # the pending save is flushed first
    assert out["nodes"]["lab"]["saved"] == {"n": 3}
    assert len(got(page, "progress")) == n_progress + 1
    assert page.evaluate("() => offs") == {"mo": 1, "mq": 1}       # synchronously, no 2 s wait
    fr.evaluate("() => { evowiki.item('a', 1, true); evowiki.save({n: 4}); }")
    settle(page, 400)
    assert got(page) == [] and len(got(page, "progress")) == n_progress + 1 and rejected(page) == 0
    assert page.evaluate("() => h.destroy()")["nodes"]["lab"]["saved"] == {"n": 3}   # idempotent
    page.close()


def test_destroy_stops_a_running_timer_and_reports_nothing_more(browser):
    b = {"type": "evoui", "title": "计时", "question": "q", "state": {}, "derived": {},
         "body": {"kind": "box", "children": [{"kind": "timer", "id": "clock", "mode": "stopwatch", "autostart": True}]},
         "fallback": "f"}
    assert validate(b) == []
    page = browser.new_page()
    page.set_content("<!doctype html><meta charset=utf-8><body><div id=m></div></body>")
    page.add_script_tag(content=JS_PATH.read_text(encoding="utf-8"))
    page.evaluate("""() => { window.ivs = 0; const c = window.clearInterval;
        window.clearInterval = function (x) { if (x != null) ivs += 1; return c.call(this, x); }; }""")
    page.evaluate(SETUP, [b, None])
    settle(page, 600)
    before = page.evaluate("() => ivs")
    out = page.evaluate("() => h.destroy()")
    assert page.evaluate("() => ivs") > before                     # the tick interval is cleared now
    assert out["nodes"]["clock"]["elapsedMs"] >= 500               # the elapsed time so far is kept
    n = len(got(page, "progress"))
    shown = page.locator("#m").inner_text()
    settle(page, 1300)
    assert len(got(page, "progress")) == n and page.locator("#m").inner_text() == shown
    page.close()
