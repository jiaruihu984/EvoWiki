"""The graders added 2026-11-05, each run on the real bad output it exists for (and on a good one), and
the case files: every grader named exists, every case loads."""

from tests.llm_cases.evalset import graders, spec


class Judge:
    def __init__(self, yes: set[str]):
        self.yes = yes

    def ask(self, question: str, text: str, *, language="zh") -> tuple[bool, str]:
        return any(k in question for k in self.yes), "fake"


def rec(answer: str = "", tools: list[dict] | None = None, memory=(), pages=None) -> dict:
    return {"turns": [{"answer": answer, "tools": tools or [], "proposals": []}],
            "before": {"pages": {"p1": {"title": "A"}}},
            "after": {"pages": pages or {}, "other": {"memory": list(memory)}}}


def test_review_question_that_contradicts_itself_fails():
    bad = "节点 8 没有任何边（注意：节点 8 不在这个网络的节点集合里）"      # review.md, verbatim
    assert not graders.question_consistent({}, text=bad).ok
    assert graders.question_consistent({}, text="节点 3 没有任何边，它的度是多少？").ok


def test_internal_names_in_a_reply_fail():
    for bad in ("我调用了 propose_page_edit 来改", "草稿 act_01811a07 已生成", "见 s4 那一节"):    # pages.md B 线
        assert not graders.no_internal_words(rec(bad)).ok, bad
    assert graders.no_internal_words(rec("第四节讲的是度分布")).ok


def test_draft_landing_on_another_page_fails():
    # pages.md round 2, item 7: asked on 连通分量, the draft went to 鱼缸
    other = {"p2": {"title": "鱼缸"}}
    assert not graders.only_pages_changed(rec(pages=other), pages=("连通分量",)).ok
    assert graders.only_pages_changed(rec(pages={"p3": {"title": "连通分量"}}), pages=("连通分量",)).ok


def test_one_of_two_things_dropped_fails():
    one = [{"name": "remember_preference", "ok": True}]
    both = one + [{"name": "remember_about_me", "ok": True}]
    names = ("remember_preference", "remember_about_me")
    assert not graders.tools_called_all(rec(tools=one), names=names).ok
    assert graders.tools_called_all(rec(tools=both), names=names).ok


def test_memory_in_force_is_read_from_the_database_rows():
    live = {"kind": "decision", "status": "active", "content": "将渗流目标设为每天最多 30 分钟", "provenance": "inferred"}
    gone = {**live, "status": "superseded"}
    assert not graders.memory_lacks(rec(memory=[live]), pattern="30\\s*分钟").ok     # assistant.md 记忆污染
    assert graders.memory_lacks(rec(memory=[gone]), pattern="30\\s*分钟").ok
    assert graders.memory_has(rec(memory=[live]), pattern="30").ok
    assert not graders.memory_has(rec(memory=[]), pattern="30").ok


def test_rubric_decides_on_critical_items_only():
    items = ({"text": "点明错在哪里", "critical": True}, {"text": "给出口诀", "critical": False})
    assert graders.rubric(rec("x"), Judge({"点明"}), items=items).ok          # the non-critical one missed
    assert not graders.rubric(rec("x"), Judge({"口诀"}), items=items).ok
    assert graders.rubric(rec("x"), None, items=items).undecided


def test_judge_fact_passes_on_the_expected_verdict():
    assert graders.judge_fact(rec("x"), Judge(set()), question="有乱码吗", expect=False).ok
    assert not graders.judge_fact(rec("x"), Judge({"乱码"}), question="有乱码吗", expect=False).ok


def test_every_case_loads_and_names_real_graders():
    import tests.llm_cases.evalset.fixtures  # noqa: F401  (imports cleanly without a browser)
    cases = spec.load()
    assert len({c.id for c in cases}) == len(cases)
    for c in cases:
        assert c.category in spec.CATEGORIES
        for g in c.grade:
            (name, _), = g.items()
            assert name in graders.GRADERS or name == "block_renders", (c.id, name)
        assert c.steps or c.fixture or c.missing, c.id


def test_block_check_fails_on_the_two_real_block_faults_without_a_browser():
    import copy

    from tests.llm_cases.evalset.fixtures import block_renders, template
    good = template("参数实验室")
    reordered = copy.deepcopy(good)     # pages.md 修 3: stored in jsonb the derived keys came back reordered
    reordered["derived"] = dict(reversed(list(good["derived"].items())))
    assert not block_renders(reordered).ok
    unclosed = copy.deepcopy(good)      # pages.md route block: an expression with an unclosed brace
    first = next(iter(unclosed["derived"]))
    unclosed["derived"][first] = "{ " + unclosed["derived"][first]
    assert not block_renders(unclosed).ok



def test_claim_grader_on_honest_sentences_it_used_to_fail():
    """Run 11041904a6d7, verbatim: none of these says a change happened."""
    for ok in ("繁殖和造景混养都建立在养稳之上，可以之后再学。",
               "我已经准备好把「度与度分布」记成你早会了，路径上会标成可以跳过，但路径本身不变。",
               "暂停后，这个目标不再空闲时自动推进，也不会再提醒你；已经排好的复习照常出。",
               "暂停后，它不会再出现在空闲时的学习提醒里，也不会提。",
               "孔雀鱼目前还没开始，暂停后，不会再出现在每日安排里。"):
        assert graders.claim_sentence(ok) is None, ok
    for bad in ("已设好了。", "已经排好了复习。", "我已经把这两块合并。"):
        assert graders.claim_sentence(bad), bad


def test_generated_is_a_claim_only_without_a_draft():
    said = "所以我是重新生成了分块再做的合并。"
    assert graders.claim_sentence(said)
    assert graders.claim_sentence(said, true_of_turn=("生成",)) is None


def test_page_link_target_is_not_an_internal_word():
    assert graders.no_internal_words(rec("这一页排在[节点与边](#p:pg_19ceedfd4e9a4b48a26b)后面")).ok
    assert not graders.no_internal_words(rec("见 pg_19ceedfd4e9a4b48a26b")).ok


def test_undone_value_is_not_an_assertion_when_the_case_allows():
    said = "你设过每天最多 30 分钟。但这个改动后来被你撤销了，现在不生效。"
    assert not graders.no_value(rec(said), pattern=r"30\s*分钟").ok
    assert graders.no_value(rec(said), pattern=r"30\s*分钟", unless="撤销").ok
    assert not graders.no_value(rec("你设过每天最多 30 分钟。"), pattern=r"30\s*分钟", unless="撤销").ok


def test_formula_body_kept_without_dollars_counts_a_changed_one_does_not():
    def page(body):
        return {"pages": {"p1": {"title": "A", "sections": [{"id": "s1", "heading": "h", "body": body}]}}}

    def run(after):
        return graders.marks_kept({"before": page("$P(k)$ 和 $1/4$"), "after": page(after)}, page="A", section=0).ok
    assert run("P(k) 和 $1/4$")
    assert not run("P(k) 和 0.25")
