"""Product acceptance checks: what a learner does, through the running service (tests/acceptance/
product.py: build_services + create_app, DBOS, Postgres, HTTP / SSE; scripted models only). One test
per product statement. A check that fails because of a known audit item is a strict xfail naming the
item (docs/product-audit-2026-10-27*.md): once the item is fixed the check passes, the XPASS fails
the run, and the marker must be removed."""

import time

import pytest

from tests.acceptance.plugin import offered_next
from tests.acceptance.product import SLOW_ANSWER, SLOW_TURN, running_product

GOAL = "我要学黑壳虾"
OWNERS = {  # assigned by 陪读助手, 2026-10-27
    "U2": "the server-progress session (local_b52b91d8)",
    "D07": "the big-goal session (local_c276ca5f)",
}

# Check 1's budget. The scripted models answer at once, so everything measured is the product's
# own work between 确认 and the first written section: the HTTP calls the chat makes, Postgres, the
# DBOS queue handing the page run to a worker. Locally that is under 1 s (much of it the queue's
# poll). Model time comes on top of this in real use (the audit's D06: minutes); 10 s leaves
# CI runners several times the local figure, and still fails as soon as a sleep, a retry back-off
# or a second queue hop lands on the path to the first section.
FIRST_SECTION_BUDGET_S = 10.0


def _running(db_url, monkeypatch, **kw):
    with running_product(db_url, monkeypatch, **kw) as p:
        yield p
        assert p.models.unscripted == [], "a model call nobody scripted: the check proves nothing"


@pytest.fixture
def product(db_url, monkeypatch):
    yield from _running(db_url, monkeypatch)


@pytest.fixture
def slow_writing(db_url, monkeypatch):
    """The product with a page writer that takes 1 s a section (a page can be stopped mid-way)."""
    yield from _running(db_url, monkeypatch, write_delay=1.0)


def known(item: str):
    """A check that fails today because of audit item `item`: a failed assertion is expected (an
    error of the harness is not), and a pass fails the run until the marker is removed."""
    return pytest.mark.xfail(strict=True, raises=AssertionError, reason=f"{item} — owner: {OWNERS[item]}")


def test_the_first_section_of_a_confirmed_goal_is_readable_within_the_budget(product):
    p = product
    card = p.ask_for_goal(GOAL)
    calls_before = len(p.models.served()) + len(p.writer.calls)
    start = time.monotonic()
    shown = p.confirm(card)                                   # 确认 on the goal card
    path = p.until_first_path(shown, "idk")                   # the chat's diagnosis: 不知道
    with p.open_page(path["path_id"]) as (_page_id, events):
        first = next(e for e in events if e.name == "section_delta" and e.data["body"].strip())
        elapsed = time.monotonic() - start
    calls = len(p.models.served()) + len(p.writer.calls) - calls_before
    print(f"\nconfirm -> first readable section: {elapsed:.2f} s, {calls} model calls by then, background ones included "
          f"(budget {FIRST_SECTION_BUDGET_S:.0f} s)")
    assert first.data["section_id"]
    assert elapsed < FIRST_SECTION_BUDGET_S


def _folded(events) -> str:
    """The answer text as the plugin folds it: each text piece carries its position (`at`)."""
    text = ""
    for e in events:
        if e.name == "text_delta":
            text = text[:e.data.get("at", len(text))] + e.data["text"]
    return text


def test_a_reload_while_the_answer_streams_shows_the_whole_answer(product):
    p = product
    sid = p.new_chat()
    with p.stream("POST", f"/assistant/sessions/{sid}/messages",
                  {"text": SLOW_TURN + "黑壳虾是什么"}) as events:
        before = [next(e for e in events if e.name == "text_delta")]
    # Obsidian reloaded mid-answer: the plugin reads the stored conversation, finds the question
    # without an answer and follows its turn again from the start (controller.ts restoreConv →
    # reattach: GET …/events?last_event_id=<message id>:0)
    stored = p.get(f"/assistant/sessions/{sid}")
    assert [m["role"] for m in stored["messages"]] == ["user"], "the turn was not still running"
    question = stored["messages"][-1]["message_id"]
    with p.stream("GET", f"/assistant/sessions/{sid}/events?last_event_id={question}:0") as events:
        after = list(events)
    assert before and after[-1].name == "done", [e.name for e in after]
    assert _folded(after) == SLOW_ANSWER
    final = p.get(f"/assistant/sessions/{sid}")["messages"]
    assert [m["role"] for m in final] == ["user", "assistant"]
    assert final[-1]["text"] == SLOW_ANSWER


def test_stop_ends_the_page_being_written(slow_writing):
    p = slow_writing
    path = p.until_first_path(p.confirm(p.ask_for_goal(GOAL)), "idk")
    with p.open_page(path["path_id"]) as (page_id, events):
        next(e for e in events if e.name == "section_delta")     # the first section is shown
        # 停止, as the plugin does it (wiki/controller/page.ts stop()): cancel the page's run on the
        # server, then stop reading the stream (leaving this block closes it)
        p.post(f"/wiki/pages/{page_id}/run/cancel", ok=(204,))
    stopped = time.monotonic()
    time.sleep(4.0)                  # the two sections left would take 2 s at 1 s a section
    # at most the section being written when 停止 was pressed is finished; nothing after it
    assert p.writer.calls_for(page_id, after=stopped) <= 1, "the page went on being written"
    writes = p.get("/wiki/writes", page_ids=page_id)
    assert all(w["state"] not in ("queued", "planning", "writing", "done") for w in writes), writes


def test_when_the_first_block_is_done_the_next_step_is_the_second_block(product):
    p = product
    shown = p.confirm(p.ask_for_goal(GOAL))
    blocks = [n for n in (shown["map"] or {}).get("nodes", []) if n["kind"] == "region"]
    assert len(blocks) == 6, [b["name"] for b in blocks]
    second = blocks[1]
    map_id = (shown.get("map") or {}).get("map_id") or shown["map_id"]
    out, paths = shown, []
    # block 1 (spec 2026-10-28 §2.2): entering it shows its parts; each part the learner opens
    # gets its own path — read every page of each, 学完 on each, until the next step leaves it
    for _ in range(12):
        path = p.until_first_path(out, "idk")
        m = p.get(f"/wiki/maps/{map_id}")
        owner = next(n for n in m["nodes"] if n["path_id"] == path["path_id"])
        assert owner["parent"] == blocks[0]["id"], owner      # a point of block 1
        for _page in path["pages"]:
            page_id = p.read_through(path["path_id"])
            p.finish(page_id)
        last = p.get(f"/wiki/paths/{path['path_id']}")
        assert last["next_index"] is None and all(x["status"] == "done" for x in last["pages"])
        paths.append(last)
        if p.get(f"/wiki/maps/{map_id}")["next_region"] == second["id"]:
            break
        out = {"map_id": map_id}
    catalog = p.get("/wiki")
    assert catalog["maps"][0]["next_region"] == second["id"]          # the server knows it
    offered = offered_next(catalog, paths, p.post(f"/wiki/pages/{page_id}/open"), p.get("/wiki/progress"))
    print(f"\nafter block 1: 今天 offers {offered['today']}, the last page offers {offered['end']}"
          f" ({len(p.conflicts)} clicks met a 409 and were clicked again)")
    assert offered["end"]["kind"] != "goal-done", "the last page says the whole goal is learned"
    assert second["name"] in str(offered["today"]), "今天 does not lead to the second block"


def test_a_learner_who_said_zero_background_gets_at_most_one_diagnosis_round(product):
    p = product
    shown = p.confirm(p.ask_for_goal(f"{GOAL}\n背景：完全零基础，没养过虾"))
    rounds: list[dict] = []
    p.until_first_path(shown, "idk", rounds)                  # 不知道 to every question (chat)
    print(f"\n零基础: {len(rounds)} diagnosis questions asked in the chat")
    assert len(rounds) <= 1
