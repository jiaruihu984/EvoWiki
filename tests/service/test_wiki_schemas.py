from typing import get_args

import pytest
from pydantic import ValidationError

from evowiki.goals import fold
from evowiki.wikiapp import wiki_schemas as ws


def test_question_has_no_answer_field():
    assert "answer" not in ws.QuestionOut.model_fields
    q = ws.QuestionOut(index=0, cell="c1.notation", concept_id="c1", concept_name="z 分数",
                       aspect="notation",
                       stem="z 的分母是？", options=["均值", "标准差", "方差", "样本量"])
    assert "answer" not in q.model_dump()


def test_answers_in_needs_exactly_one_of_answers_or_skip():
    ws.AnswersIn(answers=[0, -1])
    ws.AnswersIn(skip=True)
    with pytest.raises(ValidationError):
        ws.AnswersIn()
    with pytest.raises(ValidationError):
        ws.AnswersIn(answers=[0], skip=True)
    with pytest.raises(ValidationError):
        ws.AnswersIn(answers=[-2])


def test_inputs_forbid_extra_fields():
    for model, ok in [(ws.FeedbackIn, {"kind": "know"}),
                      (ws.FoldIn, {"folded": True}), (ws.PinIn, {"pinned": True}),
                      (ws.CheckAnswerIn, {"choice": 1}),
                      (ws.DeclareIn, {"value": None}), (ws.SettingsIO, {"review_per_day": 3}),
                      (ws.GuidelineIn, {"text": "先给例子"}),
                      (ws.ReviewGradeIn, {"cell": "c1.term", "grade": "recalled"})]:
        model(**ok)
        with pytest.raises(ValidationError):
            model(**ok, extra_field=1)


def test_draft_or_page():
    notes = ws.DraftNotesOut(from_background=2, reused_concepts=1, dropped_questions=0)
    ws.GoalDraftOut(draft_id="d1", page_id=None, title="t", concepts=[], questions=[], notes=notes)
    ws.GoalDraftOut(draft_id=None, page_id="p1", title="t", concepts=[], questions=[], notes=notes)
    with pytest.raises(ValidationError):
        ws.GoalDraftOut(draft_id=None, page_id=None, title="t", concepts=[], questions=[], notes=notes)


def test_guideline_in_needs_text():
    ws.GuidelineIn(text="x")
    with pytest.raises(ValidationError):
        ws.GuidelineIn()


def test_sse_event_names_are_fixed():
    assert ws.ORGANIZE_EVENTS == ("planning", "outline", "writer", "section_added", "section_delta",
                                  "section_done", "done", "error")


def test_settings_page_limit_and_no_curate_or_refine():
    for gone in ({"curate": "goal", "days": 7},             # 自动整理 is gone
                 {"refine": {"auto": "all", "per_page_limit_usd": 1.0}}):
        with pytest.raises(ValidationError):
            ws.SettingsIO.model_validate(gone)
    s = ws.SettingsIO.model_validate({"page_limit_usd": 0.5})
    assert s.page_limit_usd == 0.5 and "refine" not in s.model_dump()
    with pytest.raises(ValidationError):
        ws.SettingsIO(page_limit_usd=0.01)


def test_payload_maps_match_event_names():
    assert tuple(ws.ORGANIZE_PAYLOADS) == ws.ORGANIZE_EVENTS


def test_validator_messages_are_chinese():
    with pytest.raises(ValidationError, match="请给出 answers，或 skip=true"):
        ws.AnswersIn()
    with pytest.raises(ValidationError, match="答案必须是选项序号或 -1"):
        ws.AnswersIn(answers=[-2])
    notes = ws.DraftNotesOut(from_background=0, reused_concepts=0, dropped_questions=0)
    with pytest.raises(ValidationError, match="只能四选一：draft_id、page_id、path_id 或 map_id"):
        ws.GoalDraftOut(draft_id="d", page_id="p", title="t", concepts=[], questions=[], notes=notes)


def test_degenerate_inputs_rejected():
    with pytest.raises(ValidationError):
        ws.AnswersIn(answers=[])
    for blank in ["", "   ", "\n\t"]:
        with pytest.raises(ValidationError):
            ws.GuidelineIn(text=blank)
    with pytest.raises(ValidationError, match="原则不能为空"):
        ws.GuidelineIn(text="  ")
    notes = ws.DraftNotesOut(from_background=0, reused_concepts=0, dropped_questions=0)
    q = ws.QuestionOut(index=0, cell="c.term", concept_id="c", concept_name="n", aspect="term",
                      stem="s", options=["a", "b"])
    with pytest.raises(ValidationError):
        ws.GoalDraftOut(draft_id=None, page_id="p", title="t", concepts=[], questions=[q], notes=notes)
    ws.GoalDraftOut(draft_id="d", page_id=None, title="t", concepts=[], questions=[q], notes=notes)


def test_unresolved_conflicts_as_organizer_messages():
    for model in (ws.ChangeOut, ws.PageOut):
        assert "unresolved" not in model.model_fields
        assert "banner" not in model.model_fields
    c = ws.ChangeOut()
    assert c.unresolved_conflicts == []
    msg = "「z 分数·公式记号」还不够"
    c = ws.ChangeOut(unresolved_conflicts=[msg])
    assert c.model_dump()["unresolved_conflicts"] == [msg]
    p = ws.PageOut(page_id="p", title="t", goal="g", organized=True, unresolved_conflicts=[msg])
    assert p.unresolved_conflicts == [msg]
    with pytest.raises(ValidationError):
        ws.PageOut(page_id="p", title="t", goal="g", organized=True,
                   unresolved_conflicts=[{"kind": "uncovered"}])


def test_change_out_is_structured():
    c = ws.ChangeOut()
    assert c.model_dump() == {"rewritten": [], "inserted": [], "moved": [], "folded": [],
                              "unfolded": [], "llm_calls": 0, "other_pages_stale": [],
                              "unresolved_conflicts": [], "conflicts": [],
                              "budget_exceeded": False}
    c = ws.ChangeOut(rewritten=["s1"], inserted=["s9"], other_pages_stale=["p2"], llm_calls=2)
    assert c.inserted == ["s9"] and c.other_pages_stale == ["p2"] and c.llm_calls == 2


def test_cell_change_serializes_from_alias():
    a = ws.CellChangeOut(cell="c1.term", **{"from": "gap"}, to="known")
    d = a.model_dump()
    assert "from" in d and "from_" not in d
    assert d == {"cell": "c1.term", "from": "gap", "to": "known"}
    b = ws.CellChangeOut(cell="c1.term", from_="gap", to="known")
    assert b == a
    assert '"from":"gap"' in b.model_dump_json()
    assert ws.CellChangeOut.model_validate_json(b.model_dump_json()) == b


def test_diff_note_defaults_empty():
    d = ws.DiffNoteOut()
    assert d.cell_changes == [] and d.folded == [] and d.unfolded == [] and d.patched == []
    p = ws.PageOut(page_id="p", title="t", goal="g", organized=True)
    assert p.diff is None and p.llm_calls == 0
    p = ws.PageOut(page_id="p", title="t", goal="g", organized=True,
                   diff=ws.DiffNoteOut(cell_changes=[ws.CellChangeOut(cell="c", from_="gap",
                                                                      to="shaky")]))
    assert p.model_dump()["diff"]["cell_changes"] == [{"cell": "c", "from": "gap", "to": "shaky"}]


def test_done_shelve_guideline_error_shapes():
    assert ws.DoneOut().model_dump() == {}
    e = ws.ShelveOut(change=ws.ChangeOut(unfolded=["s1"]))
    assert e.change.unfolded == ["s1"]
    g = ws.GuidelineOut(guideline_id="g1", text="先给例子", source="feedback", evidence="说过两次")
    assert g.evidence == "说过两次"
    assert ws.GuidelineOut(guideline_id="g2", text="x", source="user").evidence is None
    err = ws.ErrorEvent(code="llm_failed", retryable=True)
    assert err.detail == ""
    with pytest.raises(ValidationError):
        ws.ErrorEvent(code="oops", retryable=False)
    for gone in ("topics", "pending", "history", "changes_today", "curate_run"):
        assert gone not in ws.CatalogOut.model_fields, gone


def test_catalog_carries_names():
    assert ws.CatalogOut.model_fields["names"].annotation == dict[str, str]


def test_no_assembled_text_fields():
    out_models = [m for name, m in vars(ws).items()
                  if isinstance(m, type) and issubclass(m, ws.BaseModel)
                  and name.endswith(("Out", "Event"))]
    assert out_models
    for m in out_models:
        for banned in ("note", "banner", "hint", "diff_note", "calls"):
            if (m, banned) == (ws.GoalNoteOut, "note"):     # the page-fit model's own text, not assembled
                continue
            assert banned not in m.model_fields, (m.__name__, banned)
    assert "message" not in ws.ErrorEvent.model_fields
    assert "src" not in ws.GuidelineOut.model_fields
    for m in (ws.ChangeOut, ws.PageOut, ws.OrganizeDoneEvent, ws.RewriteDoneEvent):
        assert "llm_calls" in m.model_fields, m.__name__


def test_module_header_documents_sse_and_endpoints():
    import inspect

    assert "SSE streams end (connection closed) right after `done` or `error`" in ws.__doc__
    src = inspect.getsource(ws)
    assert "# POST /wiki/pages/{id}/expand → ShelveOut" in src
    assert "/wiki/curate" not in src
    assert "/wiki/guidelines/proposal" not in src


def test_guideline_delete_is_listed():
    import inspect

    src = inspect.getsource(ws)
    assert "# DELETE /wiki/guidelines/{id} → 204" in src
    assert "TODO(carry-over): structured {section, cell, kind} later" not in src
    assert src.count("# conflicts: structured twin of unresolved_conflicts (K2)") == 2


def test_labels_are_structured():
    for m in (ws.CellOut, ws.QuestionOut, ws.CellBeliefOut):
        assert "label" not in m.model_fields and "cell_label" not in m.model_fields, m.__name__
        assert "concept_id" in m.model_fields and "aspect" in m.model_fields, m.__name__
    for m in (ws.CellOut, ws.QuestionOut):
        assert "concept_name" in m.model_fields, m.__name__
    c = ws.CellOut(cell="c1.term", concept_id="c1", concept_name="z 分数", aspect="term", band="gap")
    assert c.model_dump() == {"cell": "c1.term", "concept_id": "c1", "concept_name": "z 分数",
                              "aspect": "term", "band": "gap", "declared": None}
    with pytest.raises(ValidationError):
        ws.CellOut(cell="c1.x", concept_id="c1", concept_name="n", aspect="nonsense", band="gap")


def test_note_has_kind_and_raw_text():
    from typing import get_args

    assert set(get_args(ws.NoteOut.model_fields["kind"].annotation)) == {
        "quiz", "check", "self", "feedback", "ask", "declare", "review"}
    n = ws.NoteOut(entry_id="e1", cell="c1.term", kind="self", text="我会算 z",
                   evidence_ref="goal:g1")
    assert n.text == "我会算 z" and n.kind == "self"
    with pytest.raises(ValidationError):
        ws.NoteOut(entry_id="e1", cell="c1.term", kind="other", text="x", evidence_ref="r")


def test_changed_literal_matches_section_change():
    from typing import get_args

    from evowiki.wikiapp.events import SectionChange

    assert set(get_args(ws.Changed)) == set(get_args(SectionChange)) == {
        "cached", "rewritten", "inserted", "folded", "unfolded"}
    assert ws.SectionOut.model_fields["changed"].annotation == ws.Changed | None


def test_goal_draft_out_is_draft_page_path_or_map():
    notes = ws.DraftNotesOut(from_background=0, reused_concepts=0, dropped_questions=0)
    ws.GoalDraftOut(draft_id=None, page_id=None, path_id="path-abc", title="t", concepts=[],
                    questions=[], notes=notes)
    ws.GoalDraftOut(draft_id=None, page_id=None, map_id="map-abc", title="t", concepts=[],
                    questions=[], notes=notes)
    with pytest.raises(ValidationError, match="只能四选一：draft_id、page_id、path_id 或 map_id"):
        ws.GoalDraftOut(draft_id="d", page_id=None, path_id="path-abc", title="t", concepts=[],
                        questions=[], notes=notes)
    with pytest.raises(ValidationError, match="只能四选一"):
        ws.GoalDraftOut(draft_id=None, page_id=None, path_id="p", map_id="m", title="t",
                        concepts=[], questions=[], notes=notes)
    q = ws.QuestionOut(index=0, cell="c.term", concept_id="c", concept_name="n", aspect="term",
                       stem="s", options=["a", "b"])
    for finished in ({"path_id": "p"}, {"page_id": "p"}, {"map_id": "m"}):
        with pytest.raises(ValidationError, match="只有等待作答的草稿才带题目"):
            ws.GoalDraftOut(**{"draft_id": None, "page_id": None, **finished}, title="t",
                            concepts=[], questions=[q], notes=notes)
    selfq = ws.QuestionOut(index=0, kind="self_report", target="c", cell="", concept_id="c",
                           concept_name="n", aspect=None, stem="学过吗？", options=["会", "不会"],
                           option_means=["know", "dont"])
    out = ws.GoalDraftOut(draft_id="d", page_id=None, title="t", concepts=[], questions=[selfq],
                          notes=notes, round=1, round_title="了解你的基础")
    assert out.model_dump()["questions"][0]["option_means"] == ["know", "dont"]


def test_path_shapes():
    from typing import get_args

    from evowiki.goals.path import PageStatus

    assert set(get_args(ws.PathPageStatus)) == {s.value for s in PageStatus}
    page = ws.PathPageOut(index=1, aim="学完这页你能算 z", status="not_started", page_id=None,
                          concepts=[ws.PathConceptOut(concept_id="c1", name="z 分数", target=True)])
    out = ws.PathOut(path_id="path-1", goal="学统计", source="llm", pages=[page], next_index=1)
    assert out.model_dump()["pages"][0] == {
        "index": 1, "aim": "学完这页你能算 z", "status": "not_started", "page_id": None, "trashed": False,
        "concepts": [{"concept_id": "c1", "name": "z 分数", "target": True}],
        "title": "", "abstract": "", "sections": [], "exam": 0, "skipped": False, "key": "",
        "state": "planned", "progress": None}
    with pytest.raises(ValidationError):
        ws.PathPageOut(index=0, aim="x", status="learning", page_id="pg", concepts=[])
    ws.PathNextOut(path_id="path-1", index=1, page_id="pg_1")
    with pytest.raises(ValidationError):                       # 1-based, like PathPageOut
        ws.PathNextOut(path_id="path-1", index=0, page_id="pg_1")
    ws.PathPageDoneOut(path=out, done=ws.DoneOut())
    ws.PathCardOut(path_id="path-1", goal="学统计", pages=3, done=1, next_index=3)


def test_review_shapes():
    from typing import get_args

    from evowiki.goals.review import Grade, Period

    assert ws.Grade is Grade and ws.Period is Period
    assert set(get_args(ws.ReviewMode)) == {"cue", "check"}
    assert "answer" not in ws.ReviewCheckOut.model_fields          # key stays server-side
    item = ws.ReviewItemOut(card_id="rc_1", cell="c1.term", cells=["c1.term"], concept_id="c1", concept_name="z 分数", aspect="term",
                            band="shaky", mode="cue", page_id="pg_1", page_title="z 分数",
                            section_id="s2", heading="z 怎么读", cue="离均值几个标准差",
                            recall_now=0.55, days_to_threshold=-0.5, in_goal=True)
    today = ws.ReviewTodayOut(items=[item], due=3, done_today=0, limit=10)
    ws.DigestOut(period="day", start="2026-10-07T09:00:00Z", end="2026-10-08T09:00:00Z",
                 progressed=[], slipped=[], fading=[],
                 reviews=ws.ReviewCountsOut(recalled=1, fuzzy=0, forgot=0), today=today)
    b = ws.BandChangeOut(cell="c1.term", concept_id="c1", concept_name="z", aspect="term",
                         **{"from": "known"}, to="shaky", forgot=True)
    assert b.model_dump()["from"] == "known"


def test_review_grade_in_rules():
    ws.ReviewGradeIn(cell="c1.term", grade="recalled")
    ws.ReviewGradeIn(cell="c1.term", grade="forgot", page_id="pg_1", section_id="s2")
    ws.ReviewGradeIn(cell="c1.term", choice=-1, page_id="pg_1", section_id="s4")
    with pytest.raises(ValidationError, match="请给出 grade，或 choice"):
        ws.ReviewGradeIn(cell="c1.term")
    with pytest.raises(ValidationError, match="请给出 grade，或 choice"):
        ws.ReviewGradeIn(cell="c1.term", grade="fuzzy", choice=0, page_id="p", section_id="s")
    with pytest.raises(ValidationError, match="答确认题要给出 page_id 和 section_id"):
        ws.ReviewGradeIn(cell="c1.term", choice=0)
    with pytest.raises(ValidationError, match="给出 section_id 时也要给出 page_id"):
        ws.ReviewGradeIn(cell="c1.term", grade="fuzzy", section_id="s2")
    with pytest.raises(ValidationError):
        ws.ReviewGradeIn(cell="c1.term", grade="meh")
    with pytest.raises(ValidationError):
        ws.ReviewGradeIn(cell="c1.term", choice=-2, page_id="p", section_id="s")
    with pytest.raises(ValidationError):
        ws.ReviewGradeIn(cell="c1.term", grade="recalled", extra=1)


def test_settings_review_per_day():
    assert ws.SettingsIO().review_per_day == 10
    ws.SettingsIO(review_per_day=0)
    ws.SettingsIO(review_per_day=500)                      # no upper cap (learning-review)
    for bad in (-1,):
        with pytest.raises(ValidationError):
            ws.SettingsIO(review_per_day=bad)


def test_catalog_paths():
    assert ws.CatalogOut.model_fields["paths"].default == []
    done = ws.OrganizeDoneEvent(page_id="p", llm_calls=3)
    assert "curate_queued" not in done.model_dump() and "curated" not in done.model_dump()


def test_new_fields_default_and_old_payloads_still_validate():
    from evowiki.wikiapp.wiki_schemas import ChangeOut, ConflictOut, NoteOut

    assert ChangeOut().conflicts == [] and ChangeOut().budget_exceeded is False
    n = NoteOut(entry_id="m_1", cell="c:a", kind="quiz", text="选项", evidence_ref="quiz:d:0")
    assert n.label is None
    assert ConflictOut(kind="assumed_lost", cell="c:a").section_id is None


def test_conflict_kind_is_a_fixed_code():
    from typing import get_args

    assert set(get_args(ws.ConflictKind)) == {
        "line_weak", "pinned_line_weak", "assumed_lost", "uncovered_untaught", "uncovered_absent"}
    with pytest.raises(ValidationError):
        ws.ConflictOut(kind="其他")


def test_page_out_conflicts_and_budget_default():
    p = ws.PageOut(page_id="p", title="t", goal="g", organized=True)
    assert p.conflicts == [] and p.budget_exceeded is False
    p = ws.PageOut(page_id="p", title="t", goal="g", organized=True, budget_exceeded=True,
                   conflicts=[{"kind": "line_weak", "section_id": "s1", "cell": "c1.term"}])
    assert p.conflicts[0].section_id == "s1" and p.budget_exceeded is True


def test_conflict_kind_matches_goals_conflict_code():
    """views.conflicts_out feeds ConflictRef.kind into ConflictOut: a drift is a 500."""
    assert get_args(ws.ConflictKind) == get_args(fold.ConflictCode)


def test_teach_shapes_and_header():
    import inspect

    from pydantic import TypeAdapter

    src = inspect.getsource(ws)
    for line in ("# GET /wiki/teach/spec → TeachSpecOut",
                 "# POST /wiki/teach/items[?base_version=] SpecItemIn → TeachSpecOut (201",
                 "# PUT /wiki/teach/params[?base_version=] RepParams → TeachSpecOut",
                 "# DELETE /wiki/teach/exemplars/{id}[?base_version=] → TeachSpecOut"):
        assert line in src
    for model, good in [(ws.SpecItemIn, {"text": "x"}), (ws.SpecItemPatchIn, {"scope": "any"}),
                        (ws.VaultNoteExemplarIn,
                         {"kind": "vault_note", "ref": "a.md", "title": "A", "text": "t"})]:
        model(**good)
        with pytest.raises(ValidationError):
            model(**good, extra_field=1)
    with pytest.raises(ValidationError):
        ws.SpecItemPatchIn()
    with pytest.raises(ValidationError):
        ws.SpecItemPatchIn(text=None)
    assert ws.SpecItemPatchIn(domain=None).model_fields_set == {"domain"}
    ex = TypeAdapter(ws.ExemplarIn)
    assert isinstance(ex.validate_python({"kind": "wiki_page", "ref": "pg_1"}),
                      ws.WikiPageExemplarIn)
    with pytest.raises(ValidationError):
        ex.validate_python({"kind": "wiki_page", "ref": "pg_1", "text": "x"})
    with pytest.raises(ValidationError):
        ex.validate_python({"kind": "vault_note", "ref": "a.md", "title": "A",
                            "text": "x" * 60_001})
