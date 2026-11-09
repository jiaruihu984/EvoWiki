"""What a diff adds, and text about one case. The sentences are the real ones of the 2026-11-04 audit."""

from evowiki.harness.diff import KeyOp, PackDiff
from evowiki.harness.leak import CaseWords, added_only, added_text, leaks
from evowiki.rules.guard import Candidate, harness_candidates
from evowiki.teach.contracts import SpecPatchOp

DEFAULT = "可以使用例子、对比。不强制使用特定的开头、讲解顺序或结尾。学习者说没懂时换一种讲法。"
NEW = "可以使用例子、对比。解释涉及数量时配一个带具体数字的小例子。不强制使用特定的开头、讲解顺序或结尾。学习者说没懂时换一种讲法。"
PER_ITEM = ("遇到『先讲公式再给例子』这类相反的旧说法，直接调用 resolve_memory_conflict 保留前者、"
            "撤下后者，不要再问")
WORDS = CaseWords(titles=("度与度分布",), items=("先讲公式再给例子", "先给带具体数字的小例子，再讲公式"),
                  said=("回答太笼统，没有带具体数字我算不起来",))


def _key(text: str) -> PackDiff:
    return PackDiff((KeyOp("H1.assistant", text),))


def test_only_the_added_sentence_counts_not_the_prompt_around_it():
    assert added_text(DEFAULT, NEW) == "解释涉及数量时配一个带具体数字的小例子。"
    out = added_only(_key(NEW), {"H1.assistant": None}, {"H1.assistant": DEFAULT})
    assert harness_candidates(out) == [Candidate("解释涉及数量时配一个带具体数字的小例子。", "any", None,
                                                 "H1.assistant")]


def test_a_value_that_only_removes_text_puts_no_rule_in_force():
    out = added_only(_key("可以使用例子、对比。"), {"H1.assistant": DEFAULT}, {})
    assert out.ops == ()


def test_the_per_memory_instruction_of_the_audit_is_refused():
    hits = leaks(_key(DEFAULT + PER_ITEM), {"H1.assistant": DEFAULT}, {}, WORDS)
    assert hits == ("引用了他的某一条记忆或规则「先讲公式再给例子」",)


def test_a_general_rule_that_says_the_same_thing_is_not():
    assert leaks(_key(NEW), {"H1.assistant": None}, {"H1.assistant": DEFAULT}, WORDS) == ()


def test_titles_ids_and_copied_words_are_refused():
    assert leaks(_key("讲度与度分布时先给数字"), {}, {}, WORDS) == ("写进了这几页的标题「度与度分布」",)
    assert leaks(_key("见 msg_2dba9b14aa4a 那次"), {}, {}, WORDS) == ("写进了记录编号 msg_2dba9b14aa4a",)
    assert leaks(_key("别回答太笼统，没有带具体数字我算不起来"), {}, {}, WORDS)[0].startswith("抄了他的原话")


def test_a_title_already_in_the_text_in_force_is_not_the_candidates():
    was = "讲度与度分布时先给数字。"
    assert leaks(_key(was + "别的。"), {"H1.assistant": was}, {}, WORDS) == ()


def test_an_example_lifted_from_a_heading_is_refused_unless_already_in_force():
    words = CaseWords(headings=("小测：写下这个朋友网络的节点集合和边集合",))
    new = "先用一个具体的小例子（比如 5 个人的朋友网络）讲清楚。"
    assert leaks(_key(new), {}, {}, words) == ("用了这几段自己的内容「朋友网络」",)
    assert leaks(_key("例如朋友网络。" + new), {"H1.assistant": "例如朋友网络。"}, {}, words) == ()


def test_a_teaching_rule_is_read_too():
    d = PackDiff(spec_ops=(SpecPatchOp(op="add_item", item_id="h1", text="讲度与度分布时先给数字"),))
    assert leaks(d, {}, {}, WORDS)
