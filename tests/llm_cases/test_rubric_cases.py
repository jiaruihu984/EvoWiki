"""Rubric judging harness (tests/llm_cases/rubric.py, purpose llm_cases.rubric_judge — test-only, not
a product purpose, so not inventoried) and the TutorBench fetch script (scripts/fetch_tutorbench.py,
never run here: no download). Offline, fakes only.

Cases: RUBRICS parsing from the recorded TutorBench shape (a string holding a JSON array of
{attributes, criteria}) plus the generic fallbacks, unknown shapes refused; the judge over a
scripted gateway (all yes, mixed, malformed / incomplete replies fail loudly); the schema of the
hand-written Chinese cases (rubric_zh.json); the fetch script's parse functions on a tiny
synthetic rows-API table; and the two product drivers being judged (assistant follow-up turn with
the earlier turn as context; 「换个讲法」 through the deep single writer in feedback mode, the
RefineInput kchange._rewrite builds plus the follow-up line) over scripted providers."""

import json

import pytest

from evowiki.assistant.contracts import MessageIn, Selection
from evowiki.assistant.loop import quotes_wrong_answer
from evowiki.goals.language import language_rule
from evowiki.goals.rewrite_knobs import REWRITE_KNOBS, feedback_instruction
from evowiki.llm.chat import ChatTurn
from evowiki.llm.gateway import Gateway, LLMParseError
from evowiki.llm.log import CallLog
from evowiki.llm.scripted import ScriptedChatProvider
from tests.assistant.test_loop import CFG
from tests.deep.fakes import DeepScript, finish, section, turn
from tests.goals.llm_script import Script
from tests.llm_cases import rubric as rb
from tests.llm_cases.test_assistant_cases import BRIDGE

ft = rb.fetch_script()

# the shape of a TutorBench RUBRICS cell (as the datasets-server rows API returns it)
RECORDED_RUBRICS = json.dumps([
    {"attributes": {"explicitness": "explicit", "objectivity": "objective",
                    "severity": "critical", "tutoring_skill": "Identifying core difficulty",
                    "eval_dimension": "Correctness"},
     "criteria": "The response must identify that the student flipped the sign."},
    {"attributes": {"explicitness": "implicit", "objectivity": "subjective",
                    "severity": "not_critical", "tutoring_skill": "Encouragement",
                    "eval_dimension": "Tone"},
     "criteria": "The response should encourage the student."},
    {"attributes": {"explicitness": "explicit", "objectivity": "objective",
                    "severity": "critical", "tutoring_skill": "Step-by-step",
                    "eval_dimension": "Correctness"},
     "criteria": "The response must give the correct value of z."},
])


def _row(task_id="t1", image=None, rubrics=RECORDED_RUBRICS, **kw):
    return {"TASK_ID": task_id, "BATCH": "b1", "SUBJECT": "Statistics",
            "PROMPT": "What is a z-score?", "IMAGE_URL": None, "Image": image,
            "UC1_INITIAL_EXPLANATION": "z = (x - mean) / sd counts standard deviations.",
            "FOLLOW_UP_PROMPT": "I got z = 1 for x below the mean, why is that wrong?",
            "RUBRICS": rubrics, "bloom_taxonomy": "Understand", **kw}


def _case():
    return rb.tutorbench_case(_row())


def _verdicts(*passed, reason="依据在第二句"):
    return {"items": [{"index": i, "passed": p, "reason": reason}
                      for i, p in enumerate(passed, 1)]}


# --- RUBRICS parsing ------------------------------------------------------------------------------

def test_recorded_shape_parses_with_attributes_and_severity():
    items = rb.parse_rubrics(RECORDED_RUBRICS)
    assert items[0].text.startswith("The response must identify")
    assert items[0].attributes["tutoring_skill"] == "Identifying core difficulty"
    assert set(items[0].attributes) == {"explicitness", "objectivity", "severity",
                                        "tutoring_skill", "eval_dimension"}
    assert [x.severity for x in items] == ["critical", "not_critical", "critical"]
    assert [x.critical for x in items] == [True, False, True]


def test_recorded_shape_also_parses_when_already_decoded():
    assert rb.parse_rubrics(json.loads(RECORDED_RUBRICS)) == rb.parse_rubrics(RECORDED_RUBRICS)


@pytest.mark.parametrize("raw", [
    ["Names the error", "Gives the right value"],
    json.dumps(["Names the error", "Gives the right value"]),
])
def test_a_list_of_strings_parses(raw):
    items = rb.parse_rubrics(raw)
    assert [x.text for x in items] == ["Names the error", "Gives the right value"]
    assert items[0].attributes == {} and items[0].severity is None


def test_criterion_like_objects_keep_weight_and_polarity():
    items = rb.parse_rubrics([{"criterion": "Names the error", "weight": 2, "polarity": "positive"},
                              {"description": "Does not lecture", "polarity": "negative"}])
    assert (items[0].text, items[0].weight, items[0].polarity) == ("Names the error", 2.0,
                                                                  "positive")
    assert (items[1].text, items[1].weight, items[1].polarity) == ("Does not lecture", None,
                                                                  "negative")


@pytest.mark.parametrize("raw", [
    None, 3, {"criteria": "x"}, [], "[]", "not json", "{\"criteria\": \"x\"}", [1, 2],
    [{"score": 1}], [{"criteria": "  "}], [""], [{"criteria": "x", "attributes": "critical"}],
    [{"criteria": "x", "weight": "heavy"}],
])
def test_unknown_rubric_shapes_are_refused(raw):
    with pytest.raises(rb.RubricFormatError):
        rb.parse_rubrics(raw)


# --- the judge -----------------------------------------------------------------------------------

def test_all_yes_scores_one_and_the_prompt_carries_the_case(tmp_path):
    s = Script().push(rb.PURPOSE, _verdicts(True, True, True))
    case = _case()
    results = rb.judge(s.gateway(tmp_path), case, "You flipped the sign; z = -1.", language="en")
    sc = rb.score(results)
    assert (sc.score, sc.critical) == (1.0, 1.0)
    assert [r.item for r in results] == list(case.rubrics)
    (req,) = s.requests(rb.PURPOSE)
    assert req.prompt_version == rb.PROMPT_VERSION and req.subject == "t1"
    for must in (case.prompt, case.initial_explanation, case.follow_up,
                 "You flipped the sign; z = -1.", "1. The response must identify",
                 "3. The response must give", "共 3 条", "理由用English写"):
        assert must in req.prompt, must


def test_mixed_verdicts_score_the_fraction_and_the_critical_rate(tmp_path):
    s = Script().push(rb.PURPOSE, _verdicts(True, True, False))
    sc = rb.score(rb.judge(s.gateway(tmp_path), _case(), "answer", language="zh"))
    assert sc.score == pytest.approx(2 / 3) and sc.critical == 0.5
    d = sc.as_dict()
    assert [x["passed"] for x in d["items"]] == [True, True, False]
    assert d["items"][2]["severity"] == "critical" and d["items"][0]["reason"] == "依据在第二句"


def test_no_critical_item_gives_no_critical_rate(tmp_path):
    case = rb.tutorbench_case(_row(rubrics=["a", "b"]))
    s = Script().push(rb.PURPOSE, _verdicts(True, False))
    sc = rb.score(rb.judge(s.gateway(tmp_path), case, "answer", language="zh"))
    assert (sc.score, sc.critical) == (0.5, None)


def test_verdicts_out_of_order_are_matched_by_index(tmp_path):
    out = {"items": [{"index": 3, "passed": False, "reason": "r3"},
                     {"index": 1, "passed": True, "reason": "r1"},
                     {"index": 2, "passed": True, "reason": "r2"}]}
    s = Script().push(rb.PURPOSE, out)
    results = rb.judge(s.gateway(tmp_path), _case(), "answer", language="zh")
    assert [(r.passed, r.reason) for r in results] == [(True, "r1"), (True, "r2"), (False, "r3")]


def test_an_unparseable_reply_fails_loudly(tmp_path):
    s = Script().push(rb.PURPOSE, "not json at all")
    with pytest.raises(LLMParseError):
        rb.judge(s.gateway(tmp_path), _case(), "answer", language="zh")


@pytest.mark.parametrize("out", [
    _verdicts(True, True),                                  # an item missing
    _verdicts(True, True, True, True),                      # one too many
    {"items": [{"index": 1, "passed": True, "reason": "r"}, {"index": 1, "passed": True,
               "reason": "r"}, {"index": 2, "passed": True, "reason": "r"}]},   # a repeat
    _verdicts(True, True, True, reason=" "),                # no reason
])
def test_an_incomplete_reply_fails_loudly(tmp_path, out):
    s = Script().push(rb.PURPOSE, out)
    with pytest.raises(rb.RubricJudgeError):
        rb.judge(s.gateway(tmp_path), _case(), "answer", language="zh")


def test_an_empty_response_is_refused_before_any_call(tmp_path):
    s = Script()
    with pytest.raises(rb.RubricJudgeError):
        rb.judge(s.gateway(tmp_path), _case(), "  ", language="zh")
    assert s.calls() == 0


def test_score_of_nothing_is_an_error():
    with pytest.raises(rb.RubricJudgeError):
        rb.score([])


# --- the Chinese cases fixture -------------------------------------------------------------------

def test_zh_fixture_schema():
    cases = rb.zh_cases()
    assert 6 <= len(cases) <= 10
    assert all(4 <= len(c.rubrics) <= 8 for c in cases)
    assert all(c.rubrics[0].attributes.get("severity") for c in cases)        # TutorBench shape
    assert all(any(x.critical for x in c.rubrics) for c in cases)
    wrong = [c for c in cases if c.wrong_quote]
    assert len(wrong) >= 2
    for c in wrong:
        assert quotes_wrong_answer(MessageIn(text=c.follow_up, selection=Selection(
            kind="wrong_answer", text=c.wrong_quote)))
    assert len([c for c in cases if c.kind == "rewrite"]) >= 2
    assert all(c.kind in ("assistant", "both") for c in wrong)


def test_wrong_quote_is_the_followup_quote_layout():
    q = rb.wrong_quote("z = −1 表示什么？", ["高", "低"], "高", 1)
    assert q == "题目：z = −1 表示什么？\n选项：\n1) 高\n2) 低\n我选的：高\n正确答案：低"


def _good():
    return json.loads(rb.ZH_CASES.read_text(encoding="utf-8"))[0]


@pytest.mark.parametrize("change", [
    lambda c: c.pop("follow_up"),
    lambda c: c.update(extra=1),
    lambda c: c.update(kind="page"),
    lambda c: c.update(rubrics=c["rubrics"][:3]),
    lambda c: c.update(rubrics=c["rubrics"] * 2),
    lambda c: c["wrong"].update(chosen=c["wrong"]["options"][c["wrong"]["answer"]]),
    lambda c: c["wrong"].update(answer=9),
    lambda c: c.update(prompt=" "),
])
def test_a_bad_zh_case_is_refused(change):
    c = _good()
    change(c)
    with pytest.raises(rb.RubricFormatError):
        rb.zh_case(c)


def test_repeated_zh_case_ids_are_refused(tmp_path):
    p = tmp_path / "cases.json"
    p.write_text(json.dumps([_good(), _good()], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(rb.RubricFormatError):
        rb.zh_cases(p)


# --- the fetch script (parse functions only; nothing is downloaded) -------------------------------

def _page(rows, total):
    return {"features": [], "rows": [{"row_idx": i, "row": r, "truncated_cells": []}
                                     for i, r in enumerate(rows)],
            "num_rows_total": total, "num_rows_per_page": 100, "partial": False}


def test_parse_rows_reads_a_rows_api_page():
    rows, total = ft.parse_rows(_page([_row("t1"), _row("t2")], 2))
    assert total == 2 and [r["TASK_ID"] for r in rows] == ["t1", "t2"]


@pytest.mark.parametrize("payload", [
    [], {"rows": []}, {"rows": "x", "num_rows_total": 1}, {"rows": [{"x": 1}], "num_rows_total": 1},
    _page([{"TASK_ID": "t1", "PROMPT": "p"}], 1),
])
def test_parse_rows_refuses_other_shapes(payload):
    with pytest.raises(ft.RowsFormatError):
        ft.parse_rows(payload)


def test_image_rows_are_split_off():
    rows = [_row("t1"), _row("t2", image={"src": "https://x/i.png"}),
            _row("t3", IMAGE_URL="https://x/j.png"), _row("t4", image="")]
    text, images = ft.split_rows(rows)
    assert [r["TASK_ID"] for r in text] == ["t1", "t4"] and images == ["t2", "t3"]


def test_fetch_pages_until_the_total():
    pages = [_page([_row(f"t{i}") for i in range(100)], 130),
             _page([_row(f"t{i}") for i in range(100, 130)], 130)]
    urls = []

    def get(url):
        urls.append(url)
        return pages[len(urls) - 1]
    assert len(ft.fetch(get)) == 130
    assert "offset=0&length=100" in urls[0] and "offset=100&length=100" in urls[1]


def test_fetch_refuses_an_empty_page_before_the_total():
    with pytest.raises(ft.RowsFormatError):
        ft.fetch(lambda url: _page([], 5))


def test_rubrics_shape_describes_the_recorded_shape():
    s = ft.rubrics_shape(RECORDED_RUBRICS)
    assert s.startswith("str holding list of 3") and "criteria" in s and "severity" in s


def _rows(tmp_path, rows):
    p = tmp_path / "rows.json"
    p.write_text(json.dumps(rows), encoding="utf-8")
    return p


def test_tutorbench_cases_skip_and_count_image_rows(tmp_path):
    got = rb.tutorbench_cases(_rows(tmp_path, [_row("t1"), _row("t2", image={"src": "x"})]))
    assert [c.case_id for c in got.cases] == ["t1"] and got.image_rows == ["t2"]
    assert got.cases[0].follow_up.startswith("I got z = 1") and got.cases[0].kind == "both"
    assert got.skipped == []


def test_tutorbench_rows_split_by_flow_and_record_the_skips(tmp_path):
    """A tiny table shaped like the real one: explanation rows (both flows), follow-up rows with
    the explanation empty / null (assistant only, the rewrite skip recorded), an image row."""
    rows = [_row("t1"), _row("t2", UC1_INITIAL_EXPLANATION=None),
            _row("t3", UC1_INITIAL_EXPLANATION="   "), _row("t4", image={"src": "x"}),
            _row("t5")]
    got = rb.tutorbench_cases(_rows(tmp_path, rows))
    assert [(c.case_id, c.kind) for c in got.cases] == [
        ("t1", "both"), ("t2", "assistant"), ("t3", "assistant"), ("t5", "both")]
    assert got.flow_count("assistant") == 4 and got.flow_count("rewrite") == 2
    assert got.image_rows == ["t4"]
    assert [s.as_dict() for s in got.skipped] == [
        {"task_id": t, "flow": "rewrite", "reason": "UC1_INITIAL_EXPLANATION missing or empty"}
        for t in ("t2", "t3")]
    assert got.cases[1].initial_explanation == ""


def test_a_row_usable_by_no_flow_fails_loudly(tmp_path):
    p = _rows(tmp_path, [_row("t1"), _row("t2", FOLLOW_UP_PROMPT="")])
    with pytest.raises(rb.RubricFormatError, match="t2.*no usable fields"):
        rb.tutorbench_cases(p)


def test_a_row_with_an_unknown_rubric_shape_fails_loudly(tmp_path):
    with pytest.raises(rb.RubricFormatError):
        rb.tutorbench_cases(_rows(tmp_path, [_row("t1", rubrics={"a": 1})]))


def test_an_unparseable_rubric_fails_even_on_an_assistant_only_row(tmp_path):
    p = _rows(tmp_path, [_row("t1", UC1_INITIAL_EXPLANATION=None, rubrics="not json [")])
    with pytest.raises(rb.RubricFormatError, match="not JSON"):
        rb.tutorbench_cases(p)


def test_run_cases_runs_only_the_flows_a_row_feeds(tmp_path):
    case = rb.tutorbench_case(_row("t2", UC1_INITIAL_EXPLANATION=None))
    gw, _ = _chat_gateway(tmp_path, ChatTurn(text="Check your sign."))
    s = Script().push(rb.PURPOSE, _verdicts(True, True, True))

    class NoDeep:                    # a rewrite on an assistant-only row would touch this
        def gateway(self, *a, **k):
            raise AssertionError("the rewrite flow ran on an assistant-only row")

    res = rb.run_cases([case], chat_gateway=gw, gateway=s.gateway(tmp_path), deep_llm=NoDeep(),
                       language="en", user_id="u1")
    assert not res["errors"] and set(res["rows"][0]) == {"case", "assistant", "assistant_network"}


# --- the product calls being judged (scripted providers) ------------------------------------------

def _chat_gateway(tmp_path, *turns):
    outs = iter(turns)
    prov = ScriptedChatProvider("scripted", lambda m, r: next(outs))
    return Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record",
                   providers={"scripted": prov}), prov


def test_assistant_answers_the_follow_up_with_the_earlier_turn_as_context(tmp_path):
    gw, prov = _chat_gateway(tmp_path, ChatTurn(text="Because x is below the mean, z < 0."))
    case = _case()
    run = rb.assistant_answer(gw, case, language="en", user_id="u1")
    assert run.answer == "Because x is below the mean, z < 0." and run.last == "done"
    req = prov.chat_calls[0]
    texts = [m.text for m in req.messages]       # assistant@v5: the turn's message is the pack
    assert texts[:2] == [case.prompt, case.initial_explanation]
    assert len(texts) == 3 and texts[2].endswith("他问：\n" + case.follow_up)
    assert language_rule("en") in req.system and BRIDGE not in req.system + texts[2]
    assert not run.network


def test_a_follow_up_row_sends_the_problem_and_the_attempt_as_one_message(tmp_path):
    gw, prov = _chat_gateway(tmp_path, ChatTurn(text="Your setup is right; check h."))
    case = rb.tutorbench_case(_row(UC1_INITIAL_EXPLANATION=None))
    rb.assistant_answer(gw, case, language="en", user_id="u1")
    texts = [m.text for m in prov.chat_calls[0].messages]
    assert len(texts) == 1
    assert texts[0].endswith(f"他问：\n{case.prompt}\n\n{case.follow_up}")


def test_the_judge_prompt_says_there_was_no_explanation_for_a_follow_up_row(tmp_path):
    s = Script().push(rb.PURPOSE, _verdicts(True, True, True))
    case = rb.tutorbench_case(_row(UC1_INITIAL_EXPLANATION=None))
    rb.judge(s.gateway(tmp_path), case, "answer", language="en")
    (req,) = s.requests(rb.PURPOSE)
    assert "之前没有讲解" in req.prompt and "之前给他的讲解" not in req.prompt


def test_a_wrong_answer_case_gets_the_bridge_order(tmp_path):
    gw, prov = _chat_gateway(tmp_path, ChatTurn(text="你把方向读反了。"))
    case = next(c for c in rb.zh_cases() if c.wrong_quote)
    rb.assistant_answer(gw, case, language="zh", user_id="u1")
    s = prov.chat_calls[0].messages[-1].text      # assistant@v5: the turn's message
    assert BRIDGE in s and case.wrong_quote.splitlines()[0] in prov.chat_calls[0].messages[-1].parts[0].text
    assert BRIDGE not in prov.chat_calls[0].system


def _deep_rewrite(heading="Statistics", body="Take x = 40 with mean 50 and sd 10 ..."):
    return (DeepScript().plan()
            .write(turn(section("s1", heading=heading, cue="x 在均值以下时 z 的符号？", body=body)),
                   turn(finish())))


def test_rewrite_input_is_the_products_feedback_request_plus_the_follow_up():
    case = _case()
    inp = rb.rewrite_input(case, language="en", user_id="u1")
    product = feedback_instruction("other", "Statistics")
    assert (inp.mode, inp.scope, inp.language, inp.required) == ("feedback", ("s1",), "en", ())
    assert inp.instruction == product + "\n" + rb.FOLLOW_UP_LINE.format(follow_up=case.follow_up)
    assert inp.page.texts["s1"].body == case.initial_explanation
    assert [s.id for s in inp.page.outline.sections] == ["s1"] and inp.goal == case.prompt
    assert all(v in inp.instruction for v in REWRITE_KNOBS["other"].values())   # 换个讲法 knobs


def test_rewrite_runs_the_deep_writer_scoped_to_the_section(tmp_path):
    ds = _deep_rewrite()
    case = _case()
    run = rb.rewrite_section(ds.llm(), case, language="en", user_id="u1")
    assert run.text == "x 在均值以下时 z 的符号？\n\nTake x = 40 with mean 50 and sd 10 ..."
    assert run.sections == ["s1"] and run.rounds == 1 and run.calls == 3   # plan_page + 2 turns
    assert ds.count("deep.plan") == 0 and ds.count("deep.write") == 3 and not ds.pending()
    req = next(r for r in ds.requests if r.purpose == "deep.write")
    first = req.messages[0].text
    for must in (case.initial_explanation, case.follow_up, "换个讲法", case.prompt):
        assert must in first, must
    for request in ds.requests:
        if request.purpose == "deep.write":
            assert request.messages[-1].role == "user"
            assert language_rule("en") in request.messages[-1].text
            assert language_rule("en") not in request.system
    assert "这次只改写这些段：s1" in first


def test_run_cases_judges_each_flow_and_records_failures(tmp_path):
    case = _case()                                          # kind both: assistant + rewrite
    chat, _ = _chat_gateway(tmp_path, ChatTurn(text="z is negative below the mean."))
    s = Script().push(rb.PURPOSE, _verdicts(True, True, True), _verdicts(True, False, False))
    out = rb.run_cases([case], chat_gateway=chat, gateway=s.gateway(tmp_path),
                       deep_llm=_deep_rewrite().llm(), language="en", user_id="u1")
    (row,) = out["rows"]
    assert row["assistant"]["score"] == 1.0 and row["rewrite"]["score"] == 0.3333
    assert row["rewrite"]["sections"] == ["s1"] and row["rewrite"]["calls"] == 3
    assert out["critical_mean"] == {"assistant": 1.0, "rewrite": 0.5} and not out["errors"]


def test_run_cases_records_a_failed_judge_and_a_failed_rewrite(tmp_path):
    case = _case()
    chat, _ = _chat_gateway(tmp_path, ChatTurn(text="z is negative below the mean."))
    s = Script().push(rb.PURPOSE, "broken")
    out = rb.run_cases([case], chat_gateway=chat, gateway=s.gateway(tmp_path),
                       deep_llm=DeepScript().plan().llm(), language="en", user_id="u1")
    assert out["errors"][0]["error"].startswith("LLMParseError")
    assert [e["flow"] for e in out["errors"]] == ["assistant", "rewrite"]
    assert out["rows"] == [{"case": "t1", "assistant_network": []}]
    assert out["mean"] == {"assistant": None, "rewrite": None}


# --- error causes and resume (live TutorBench case) -----------------------------------------------

def test_an_assistant_error_event_carries_its_payload_into_the_recorded_error(tmp_path):
    from evowiki.llm.gateway import ProviderUnavailableError

    def down(m, r):
        raise ProviderUnavailableError("deepseek down for this test")
    prov = ScriptedChatProvider("scripted", down)
    gw = Gateway(CFG, CallLog(tmp_path / "c.jsonl"), mode="record", providers={"scripted": prov})
    run = rb.assistant_answer(gw, _case(), language="en", user_id="u1")
    assert run.last == "error" and run.error["code"] == "llm_unavailable"
    assert "deepseek down for this test" in run.error["detail"]
    s = Script()
    out = rb.run_cases([rb.tutorbench_case(_row(UC1_INITIAL_EXPLANATION=None))], chat_gateway=gw,
                       gateway=s.gateway(tmp_path), deep_llm=None, language="en", user_id="u1")
    (err,) = out["errors"]
    assert err["error"].startswith("AssistantTurnError: assistant turn ended with error: "
                                   "code='llm_unavailable', retryable=True, detail=")
    assert "deepseek down for this test" in err["error"] and len(err["error"]) <= 500
    assert err["kind"] == "AssistantTurnError: llm_unavailable"
    assert "AssistantTurnError: llm_unavailable ×1" in rb.error_summary(out["errors"])


def _three():
    return [rb.tutorbench_case(_row("t1")), rb.tutorbench_case(_row("t2")),
            rb.tutorbench_case(_row("t3", UC1_INITIAL_EXPLANATION=None))]   # 5 (case, flow) pairs


class FakeRunner:
    """run_flow stand-in: counts the pairs it runs, scores them `score`, fails the pairs in
    `fail`, and records one paid meter row per pair (cost 0.01) in `meter`."""

    def __init__(self, score=0.5, fail=(), meter=None):
        self.ran, self.score, self.fail, self.meter = [], score, set(fail), meter

    def __call__(self, flow, case, row):
        from evowiki.llm.usage import Usage

        self.ran.append((case.case_id, flow))
        if self.meter is not None:
            self.meter.rows.append((rb.PURPOSE, Usage(provider="p", model="m", input_tokens=1,
                                                      output_tokens=1, cost_usd=0.01), False))
        if flow == "assistant":
            row["assistant_network"] = []
        if (case.case_id, flow) in self.fail:
            raise RuntimeError("blip")
        return {"response": "r", "score": self.score, "critical": self.score, "items": []}


def _run(cases, runner, resume=None):
    return rb.run_cases(cases, chat_gateway=None, gateway=None, deep_llm=None, language="en",
                        user_id="u1", resume=resume, run_flow=runner)


def test_resume_keeps_scored_pairs_and_runs_only_the_errored_ones():
    cases = _three()
    first = _run(cases, FakeRunner(score=1.0, fail={("t1", "rewrite"), ("t3", "assistant")}))
    assert first["run_now"] == 5 and len(first["errors"]) == 2
    resume = rb.resume_from(json.loads(json.dumps(first)), cases)
    assert set(resume.reuse) == {("t1", "assistant"), ("t2", "assistant"), ("t2", "rewrite")}
    again = FakeRunner(score=0.0)
    second = _run(cases, again, resume)
    assert again.ran == [("t1", "rewrite"), ("t3", "assistant")]
    assert second["run_now"] == 2 and second["reused_count"] == 3 and not second["errors"]
    assert second["reused"] == [{"case": "t1", "flow": "assistant"},
                                {"case": "t2", "flow": "assistant"},
                                {"case": "t2", "flow": "rewrite"}]
    # the means cover reused (1.0) and new (0.0) entries
    assert second["mean"] == {"assistant": 0.6667, "rewrite": 0.5}
    assert second["critical_mean"] == {"assistant": 0.6667, "rewrite": 0.5}
    assert "3 scored (case, flow) pairs reused" in second["resume_note"]


def test_an_entry_without_a_score_is_never_reused():
    cases = _three()
    prev = _run(cases, FakeRunner(score=1.0))
    prev["rows"][0]["assistant"]["score"] = None
    prev["rows"][1]["rewrite"] = {"response": "r"}
    resume = rb.resume_from(prev, cases)
    assert ("t1", "assistant") not in resume.reuse and ("t2", "rewrite") not in resume.reuse
    runner = FakeRunner()
    _run(cases, runner, resume)
    assert runner.ran == [("t1", "assistant"), ("t2", "rewrite")]


def test_the_fresh_flag_runs_every_pair(tmp_path):

    cases = _three()
    p = tmp_path / "tutorbench.json"
    p.write_text(json.dumps(_run(cases, FakeRunner(score=1.0))), encoding="utf-8")
    prev, fresh = rb.earlier_report(p, {rb.FRESH_ENV: "1"})
    assert prev is None and fresh == "EVOWIKI_TUTORBENCH_FRESH=1"
    runner = FakeRunner()
    out = _run(cases, runner, rb.resume_from(prev, cases, fresh_reason=fresh))
    assert len(runner.ran) == 5 and out["reused_count"] == 0
    assert out["resume_note"] == "EVOWIKI_TUTORBENCH_FRESH=1: fresh run"
    prev, fresh = rb.earlier_report(p, {})                   # default: the earlier report is used
    assert fresh is None and prev["case_ids"] == ["t1", "t2", "t3"]
    assert rb.earlier_report(tmp_path / "none.json", {}) == (None, None)


def test_a_changed_case_id_set_starts_fresh_and_says_so():
    cases = _three()
    prev = _run(cases[:2], FakeRunner(score=1.0))
    resume = rb.resume_from(prev, cases)
    assert not resume.reuse
    assert resume.note.startswith("case list changed (2 earlier ids, 3 now; 1 added, 0 removed)")
    runner = FakeRunner()
    _run(cases, runner, resume)
    assert len(runner.ran) == 5
    old = {k: v for k, v in _run(cases, FakeRunner(score=1.0)).items() if k != "case_ids"}
    assert len(rb.resume_from(old, cases).reuse) == 5        # older reports: ids from the rows


def test_calls_and_cost_count_only_this_runs_calls():
    from tests.llm_cases.live.harness import Live, MemoryMeter

    cases = _three()
    first = _run(cases, FakeRunner(score=1.0, fail={("t3", "assistant")}))
    live = Live(name="tb", gateway=None, meter=MemoryMeter())
    out = _run(cases, FakeRunner(meter=live.meter), rb.resume_from(first, cases))
    spend = live.spend()
    assert spend["calls"] == 1 and spend["cost_usd"] == 0.01            # the one pair run now
    rows = rb.TutorBenchRows(cases=cases, image_rows=["t9"], skipped=[])
    extra = rb.report_extra(out, rows, {"cost_usd": 0.02, "calls": 0}, spend["cost_usd"])
    assert extra["total_cost_usd"] == 0.03 and extra["reused_count"] == 4
    assert extra["per_row"]["t3"] == {"assistant": 0.5}
    assert extra["per_row"]["t1"] == {"assistant": 1.0, "rewrite": 1.0}


def test_the_error_summary_counts_causes_by_kind():
    errors = [{"case": f"c{i}", "flow": "assistant", "kind": "AssistantTurnError: llm_unavailable",
               "error": "AssistantTurnError: assistant turn ended with error: code=..."}
              for i in range(3)]
    errors.append({"case": "c9", "flow": "rewrite",
                   "error": "WriteFailed: llm_unavailable: ProviderTransientError ..."})
    msg = rb.error_summary(errors)
    assert msg.startswith("4 errors by kind: AssistantTurnError: llm_unavailable ×3 (first: "
                          "c0/assistant: ")
    assert "WriteFailed ×1 (first: c9/rewrite" in msg
