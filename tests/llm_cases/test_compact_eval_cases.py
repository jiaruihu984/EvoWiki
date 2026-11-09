"""eval.compact.judge (compact-eval@v2, src/evowiki/evaluation/compact.py): the fixed evaluator of the
conversation summary, offline with a scripted judge. The fixed cases are
src/evowiki/evaluation/compact_cases.py.

eval.compact.judge — one JSON call on the gateway's verifier role.
  must know: the stored compaction request's input (earlier hand-over + folded dialogue with the
    user message ids), the candidate summary (every field, labelled), the fixed key points of a
    case when there are any.
  must not know: which prompt or version wrote the summary, the product's own checks.
  code, not the judge: the v7 shape, the evidence ids (from the stored request's params), the
    compression ratio; a judge quote that is not in the summary does not count."""

import json

import pytest

from evowiki.evaluation.compact import (
    COMPRESSION_MIN_INPUT,
    EVAL_PURPOSE,
    EVAL_VERSION,
    MAX_COMPRESSION,
    evaluate,
    structure,
)
from evowiki.evaluation.compact_cases import (
    CASES,
    case_request,
    check_case,
    false_learning,
    lost_points,
)
from evowiki.forge.policy import violation
from evowiki.llm.gateway import Gateway, LLMParseError
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.prompt_steps import STEPS
from evowiki.llm.scripted import ScriptedChatProvider

GEN, JUDGE = ModelRef(provider="scripted", model="writer"), ModelRef(provider="scripted", model="judge")
CFG = GatewayConfig(generator=GEN, verifier=JUDGE, allow_single_provider=True)
CASE = {c.name: c for c in CASES}


def _req(prompt="[m0] 用户：z 分数是什么\n助手：离平均几个标准差。" * 10, ids=("m0", "m1")):
    return LLMRequest(purpose="assistant.compact", prompt_version="compact@v7", subject="s1",
                      system="x", prompt=prompt, params={"evidence_ids": list(ids)})


def _reply(summary="讲了 z 分数", unclear=(), understood=(), pending=(), requests=()):
    """`pending`: what is still open, said in the summary."""
    return json.dumps({
        "handoff": {"summary": "".join((summary, *pending)), "decisions": [], "active_requests": list(requests)},
        "learning": {"unclear": [{"concept": c, "evidence_id": i} for c, i in unclear],
                     "understood": [{"concept": c, "evidence_id": i} for c, i in understood]}},
        ensure_ascii=False)


def _judge(tmp_path, out):
    def no_chat(m, r):
        raise AssertionError("the judge is a JSON call")
    prov = ScriptedChatProvider("scripted", no_chat,
                                lambda m, r: out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))
    return prov, Gateway(CFG, CallLog(tmp_path / "j.jsonl"), mode="record",
                         providers={"scripted": prov}, record_max_attempts=1)


# ---- structure (code) ----

def test_structure_flags_evidence_the_stored_request_does_not_allow():
    s = structure(_req(), _reply(unclear=[("负的 z", "m1"), ("编的", "a_m0")],
                                 understood=[("均值", "m9")]))
    assert s.schema_ok and s.learning_items == 3
    assert s.bad_evidence == ("unclear:编的 (a_m0)", "understood:均值 (m9)")
    assert 0 < s.compression < MAX_COMPRESSION


@pytest.mark.parametrize("raw", ["不是 JSON", "[]", '{"handoff": {}}',
                                 '{"handoff": {}, "learning": {}, "questions": []}',
                                                                  '{"handoff": {}, "learning": {"unclear": [{"concept": "x"}]}}'])
def test_structure_refuses_anything_but_the_v7_shape(raw):
    s = structure(_req(), raw)
    assert not s.schema_ok and s.error


def test_structure_without_the_evidence_param_allows_nothing():
    req = _req().model_copy(update={"params": {}})
    assert structure(req, _reply(unclear=[("负的 z", "m0")])).bad_evidence == ("unclear:负的 z (m0)",)


def test_a_long_input_retold_at_length_is_not_compressed():
    size = COMPRESSION_MIN_INPUT
    ev = evaluate(_req(prompt="字" * size), _reply(summary="字" * int(size * MAX_COMPRESSION + 10)))
    assert ev.compression_measured and not ev.compressed and not ev.passed and not ev.judged
    ok = evaluate(_req(prompt="字" * size), _reply(summary="字" * (size // 2)))
    assert ok.compressed and ok.passed


def test_a_short_input_is_not_held_to_the_compression_ratio():
    ev = evaluate(_req(prompt="字" * (COMPRESSION_MIN_INPUT - 1)), _reply(summary="字" * 2000))
    assert not ev.compression_measured and ev.compressed and ev.passed
    assert ev.report()["compression_measured"] is False


def test_code_only_evaluation_costs_nothing_and_passes_a_clean_reply():
    ev = evaluate(_req(), _reply(unclear=[("负的 z", "m1")]))
    assert ev.passed and not ev.judged and ev.judge_call is None and ev.retention is None


# ---- the judge ----

def test_judge_sees_the_input_and_the_whole_summary_and_runs_on_the_verifier(tmp_path):
    prov, gw = _judge(tmp_path, {"key_points": [{"point": "讲了 z 分数", "kept": True}]})
    ev = evaluate(_req(), _reply(pending=["第 2 题还没答"], requests=["先解释符号"],
                                 unclear=[("负的 z", "m1")]), gateway=gw)
    [req] = prov.calls
    assert req.purpose == EVAL_PURPOSE == "eval.compact.judge" and req.prompt_version == EVAL_VERSION
    for must in ("[m0] 用户：z 分数是什么", "交接：讲了 z 分数", "第 2 题还没答",
                 "这次对话里的要求：先解释符号", "他没懂的：负的 z"):
        assert must in req.prompt, must
    assert "compact@v7" not in req.prompt and "⟦" not in req.prompt and "⟦" not in req.system
    # an item carried over from the earlier hand-over counts as evidenced (its words are not here)
    assert "沿用下来就算有证据" in req.prompt
    assert ev.judged and ev.passed and ev.retention == 1.0
    assert ev.judge_call is not None and ev.judge_call.model == "judge"


def test_judge_findings_count_only_with_a_quote_from_the_summary(tmp_path):
    out = {"key_points": [{"point": "讲了 z 分数", "kept": True},
                          {"point": "第 2 题还没答", "kept": False}],
           "made_up": [{"quote": "他已经掌握了 z 分数", "why": "材料里没有"},
                       {"quote": "讲了 z 分数", "why": "瞎判"}],
           "missed_tasks": ["第 2 题还没答"],
           "wrong_tasks": [{"quote": "不存在的句子", "why": "x"}],
           "misfiled": [{"quote": "负的 z", "why": "他没说过不懂"}]}
    _, gw = _judge(tmp_path, out)
    ev = evaluate(_req(), _reply(unclear=[("负的 z", "m1")]), gateway=gw)
    assert ev.retention == 0.5 and not ev.passed
    assert ev.made_up == ("讲了 z 分数：瞎判",)            # the first quote is not in the summary
    assert ev.task_errors == ("漏了：第 2 题还没答", "放错了：负的 z：他没说过不懂")
    assert ev.quote_misses == 2
    rep = ev.report()
    assert rep["passed"] is False and rep["retention"] == 0.5 and rep["quote_misses"] == 2


def test_fixed_key_points_are_judged_in_order_and_a_skipped_one_is_not_kept(tmp_path):
    prov, gw = _judge(tmp_path, {"key_points": [{"point": "乙", "kept": True},
                                                {"point": "多出来的", "kept": True}]})
    ev = evaluate(_req(), _reply(), gateway=gw, key_points=("甲", "乙"))
    assert ev.key_points == (("甲", False), ("乙", True)) and ev.retention == 0.5
    assert "- 甲\n- 乙" in prov.calls[0].prompt


def test_a_reply_of_the_wrong_shape_is_never_judged(tmp_path):
    prov, gw = _judge(tmp_path, {"key_points": []})
    ev = evaluate(_req(), "问了什么：\n- z", gateway=gw)
    assert not ev.passed and not ev.judged and prov.calls == []


def test_an_unparseable_judge_reply_is_an_error_not_a_pass(tmp_path):
    _, gw = _judge(tmp_path, "通过")
    with pytest.raises(LLMParseError):
        evaluate(_req(), _reply(), gateway=gw)


# ---- protection ----

def test_the_evaluator_is_outside_every_improvement_loop():
    for path in ("src/evowiki/evaluation/compact.py", "src/evowiki/evaluation/compact_cases.py"):
        assert violation(path, exists=True)                     # no code patch may touch it
    assert all(EVAL_PURPOSE not in s.purposes for s in STEPS)    # no H1 step, no editable span


# ---- the fixed cases ----

GOOD = {
    "long_fold": _reply("z 分数是先减平均再除以标准差，把差多少分换成差几个标准差；标准差衡量离平均通常有多远。"
                        "比较语文和数学时，分布形状差很多就要小心。极端值会同时影响平均和标准差。"
                        "他把「z 分数的意义」一节改成用排名举例，这样讲可以，但 z 分数不是名次本身。"),
    "quiz_left_open": _reply("讲了 z 分数的含义和公式；出了两道题，第 1 题他答对了。",
                             understood=[("由分数求 z", "m2")],
                             pending=["第 2 题（z = −1.5 说明什么）还没答"]),
    "duplicate_questions_wait": _reply("摸底题出现了重复，助手在等他把那两道题发来；他喜欢组件多的页面，这一页先不加。",
                                       pending=["等他提供两道重复的摸底题"]),
    "said_he_understood": _reply("讲了同分不同标准差时 z 不同，他说理解了；讲了 68-95-99.7 规则，记住即可。",
                                 understood=[("不同班级比较 z 分数", "m1")]),
    "draft_not_confirmed": _reply("他想要拖动配对的组件；讲了碱基 A、T、G、C 和配对。"),
    "session_request": _reply("讲了标准差和方差的区别、z 分数的公式。", requests=["给公式前先解释每个符号"]),
    "resolved_since": _reply("他答对了第 2 题，说负的 z 懂了；为什么除以 σ 还没说懂。",
                             unclear=[("为什么除以 σ", "m1")], understood=[("负的 z", "m2")]),
}


def test_only_the_long_fold_is_measured_for_compression():
    measured = {c.name for c in CASES if c.worth_a_call
                and evaluate(case_request(c), _reply()).compression_measured}
    assert measured == {"long_fold"}


def test_every_case_builds_the_product_request_except_the_greeting():
    for c in CASES:
        req = case_request(c)
        if not c.worth_a_call:
            assert req is None, c.name
            continue
        assert req is not None and req.purpose == "assistant.compact", c.name
        assert req.prompt_version == "compact@v16" and req.params["evidence_ids"], c.name
        assert c.key_points, c.name
    assert set(GOOD) == {c.name for c in CASES if c.worth_a_call}


@pytest.mark.parametrize("name", sorted(GOOD))
def test_a_good_reply_meets_its_case(name):
    case = CASE[name]
    assert check_case(case, GOOD[name]) == []
    assert evaluate(case_request(case), GOOD[name]).passed, name


def test_a_question_cited_as_learning_evidence_is_a_false_item():
    """The live v9 reply: 「碱基是什么东西」 is a real message of his, so `evidence_id` checks, but
    it only asks. The hand labels see that; `structure` cannot."""
    case = CASE["draft_not_confirmed"]
    raw = _reply("讲了碱基。", unclear=[("碱基是什么", "m1")])
    assert structure(case_request(case), raw).bad_evidence == ()
    assert false_learning(case, raw) == ["假的 unclear：碱基是什么（m1）"]
    assert check_case(case, raw) == ["假的 unclear：碱基是什么（m1）"]
    claimed = _reply("讲了碱基。", understood=[("碱基配对", "m1")])
    assert false_learning(case, claimed) == ["假的 understood：碱基配对（m1）"]


def test_one_message_supports_only_the_point_it_makes():
    """The live v10 reply to long_fold: m2 says 「懂了」 about z = −1 and is uneasy about comparing
    subjects. An item claiming the latter passes the per-message label; this expectation sees it."""
    case = CASE["long_fold"]
    raw = _reply("讲了 z 分数。", understood=[("z 分数可以跨科目比较", "m2")])
    assert false_learning(case, raw) == []
    assert len(check_case(case, raw)) == 1
    assert check_case(case, _reply("讲了 z 分数。", understood=[("z = −1 是低一个标准差", "m2")])) == []


def test_a_summary_that_drops_what_must_survive_fails_its_case():
    """The live v9 reply to duplicate_questions_wait: an empty summary and one to-do."""
    case = CASE["duplicate_questions_wait"]
    raw = _reply("", pending=["用户把两道重复的摸底题发给助手"])
    assert lost_points(case, raw) == [("组件",), ("喜欢", "偏好", "更想要"),
                                      ("先不", "暂不", "暂时不", "不加")]
    assert len(check_case(case, raw)) == 3


def test_the_v6_mistakes_fail_their_cases():
    bad = {
        "quiz_left_open": _reply(unclear=[("z = −1.5 的含义", "m2")]),           # unanswered ≠ unclear
        "duplicate_questions_wait": _reply(unclear=[("等用户提供两道重复的题", "m0")]),
        "said_he_understood": _reply(unclear=[("68% 怎么来的", "m1")]),
        "draft_not_confirmed": _reply("生成了拖动配对的草稿"),                     # what he learned is lost
        "session_request": _reply("讲过：以后公式先解释符号"),                     # the request lost
        "resolved_since": _reply(unclear=[("负的 z 是什么意思", "m0")], pending=["第 2 题还没答"]),
    }
    for name, raw in bad.items():
        assert check_case(CASE[name], raw), name
    assert check_case(CASE["quiz_left_open"], "问了什么：- 好") != []
