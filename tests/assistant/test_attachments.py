"""Chat attachments, pure parts: placement, budget, toc, picture cap, cards (no LLM, no DB)."""

from dataclasses import replace
from datetime import timedelta

import pytest

from evowiki.assistant.attachments import (
    MAX_FIGURE_PAGES,
    TOC_MAX_TOKENS,
    Attachment,
    AttachmentFailed,
    AttachmentGone,
    AttachmentReading,
    Placement,
    attachment_out,
    budget,
    ensure_usable,
    full_text,
    limit,
    place,
    plan,
    toc_text,
    window,
)
from evowiki.goals.context import ContextOverBudget, estimate_tokens
from evowiki.sources.config import DATA_BEGIN
from tests.assistant.attach_fakes import doc, image, png, reader, sec, src
from tests.assistant.fakes import T0


def no_render(data, pages):
    raise AssertionError("no page should be rendered")


def fake_render(data, pages):
    return [png("blue") for _ in pages]


def run_plan(placement, items, *, sees, budget_tokens=10_000, limit_tokens=100_000,
             render=no_render):
    rd = reader(*items)
    atts = {a.source_id: a for a, _ in items}
    return plan(placement, atts, sees=sees, budget_tokens=budget_tokens,
                limit_tokens=limit_tokens, user_id="u1", reader=rd, render=render), rd


def test_place_first_answered_turn_wins_strays_ride_along_folded_stay_named():
    p = place([("m1", ["a", "b"], True), ("m2", ["a", "c"], True), ("m3", ["d"], False)],
              ("m4", ["e", "b"]), folded=["z", "e"])
    # d (its turn never answered) rides with m4 ahead of m4's own, but is not m4's own
    assert p.turns == (("m1", ("a", "b")), ("m2", ("c",)), ("m4", ("d",)), ("m4", ("e",)))
    assert p.carried == {"d"}
    assert p.folded == ("z",)                         # sent again later: placed, not folded


def test_place_a_stray_stays_with_the_next_answered_turn_unless_sent_again():
    turns = [("m1", ["x"], False), ("m2", [], True)]
    assert place(turns, ("m3", []), []).turns == (("m2", ("x",)), ("m3", ()))
    again = place(turns, ("m3", ["x"]), [])
    assert again.turns == (("m3", ("x",)),) and again.carried == frozenset()


def test_budget_is_threshold_or_half_window_less_history(monkeypatch):
    monkeypatch.setenv("EVOWIKI_FULL_TEXT_TOKENS", "30000")
    assert window("anthropic") == 200_000 and window("deepseek") == 128_000
    assert window("scripted") == 128_000
    assert budget("deepseek", 0) == 30_000
    assert budget("deepseek", 50_000) == 14_000          # 64000 − 50000
    monkeypatch.setenv("EVOWIKI_CONTEXT_WINDOW", "10000")
    assert window("anthropic") == 10_000 and budget("anthropic", 1000) == 4000


def test_over_budget_oldest_turn_largest_first_becomes_toc():
    old_small, old_big = doc("a", ["短" * 100]), doc("b", ["长" * 3000, "第二节开头\n后面"])
    new = doc("c", ["新" * 2000])
    pl = Placement(turns=(("m1", ("a", "b")), ("m2", ("c",))))
    p, _ = run_plan(pl, [(old_small, None), (old_big, None), (new, None)], sees=False,
                    budget_tokens=2500)
    assert {k: v.mode for k, v in p.reads.items()} == {"a": "full", "b": "toc", "c": "full"}
    toc = p.parts["m1"][1]
    assert toc.source_id == "b" and "read_source" in toc.text and DATA_BEGIN in toc.text
    assert "§b-s1 第 1 节（p.2）：第二节开头" in toc.text and "长" * 200 not in toc.text
    assert "只给了目录（2 节）和每节第一行" in p.statement
    assert "【a.pdf · source_id=a · 1 页 · 约 104 token · 整份已给出】" in p.statement
    assert p.parts["m2"][0].text.count("新") == 2000 and DATA_BEGIN in p.parts["m2"][0].text


def test_sees_images_gives_pictures_up_to_the_cap_per_message():
    old = doc("old", ["正文"], figures=tuple(range(1, 13)))    # 12 figure pages
    new = doc("new", ["正文"], figures=tuple(range(1, 10)))    # 9 figure pages
    pic, pic2 = image("pic"), image("pic2")
    pl = Placement(turns=(("m1", ("old",)), ("m2", ("new", "pic", "pic2"))))
    p, _ = run_plan(pl, [(old, b"%PDF"), (new, b"%PDF"), (pic, png()), (pic2, png())],
                    sees=True, render=fake_render)
    assert [x.kind for x in p.parts["m1"]] == ["doc", *["image"] * MAX_FIGURE_PAGES]
    assert [x.kind for x in p.parts["m2"]] == ["doc", *["image"] * 9, "image"]
    assert p.reads["old"].unseen_pages == (11, 12)
    assert p.reads["pic"].mode == "image" and p.reads["pic2"].mode == "unseen"
    assert "第 1–10 页是图，已给出页面图片 · 第 11–12 页没有读到内容" in p.statement
    assert p.parts["m2"][1].name == "new.pdf 第 1 页"


def test_an_older_picture_never_needs_eyes_it_is_named_as_seen_then():
    fig = image("fig", figure=True)
    pdf = doc("d", ["正文"], figures=(2,))
    ocr = image("ocr", ocr="旧图里的字")
    pl = Placement(turns=(("m1", ("fig", "d", "ocr")), ("m2", ())))
    p, rd = run_plan(pl, [(fig, png()), (pdf, b"%PDF"), (ocr, png())], sees=False)
    assert not p.needs_eyes and rd.files_read == []
    assert [x.kind for x in p.parts["m1"]] == ["doc", "doc"]          # pdf text + ocr text
    assert p.reads["fig"].mode == "unseen" and p.reads["d"].unseen_pages == (2,)
    assert "【fig.png · source_id=fig · 图片 · 之前那一轮已经看过，见当时的回答】" in p.statement
    assert "第 2 页是图，之前那一轮已经看过，见当时的回答" in p.statement
    assert "没有读到内容" not in p.statement


def test_pictures_count_in_the_budget():
    a = doc("a", ["字" * 1000])
    pl = Placement(turns=(("m1", ("a", "pic")),))
    p, _ = run_plan(pl, [(a, None), (image("pic"), png())], sees=True, budget_tokens=2000)
    assert p.reads["a"].mode == "toc"                    # 1000 + 1600 for the picture > 2000
    p, _ = run_plan(pl, [(a, None), (image("pic"), png())], sees=True, budget_tokens=3000)
    assert p.reads["a"].mode == "full"


def test_zero_budget_makes_everything_toc_and_over_the_limit_fails_closed():
    assert budget("deepseek", 200_000) == 0 and limit("deepseek", 100_000) == 2400
    a = doc("a", ["短"])
    p, _ = run_plan(Placement(turns=(("m1", ("a",)),)), [(a, None)], sees=False,
                    budget_tokens=0)
    assert p.reads["a"].mode == "toc"
    with pytest.raises(ContextOverBudget):
        run_plan(Placement(turns=(("m1", ("pic",)),)), [(image("pic"), png())], sees=True,
                 budget_tokens=0, limit_tokens=1000)


def test_a_long_toc_is_cut():
    a = doc("big", [f"第 {i} 节开头" + "字" * 200 for i in range(400)])
    text = toc_text(a)
    assert "目录太长，后面还有" in text and estimate_tokens(text) < TOC_MAX_TOKENS + 200


def test_without_eyes_text_from_ocr_and_a_figure_needs_eyes_before_any_file_is_read():
    ocr = image("ocr", ocr="图里写着 x² + y² = 1")
    p, _ = run_plan(Placement(turns=(("m1", ("ocr",)),)), [(ocr, png())], sees=False)
    assert not p.needs_eyes and [x.kind for x in p.parts["m1"]] == ["doc"]
    assert "x² + y² = 1" in p.parts["m1"][0].text and p.reads["ocr"].mode == "text_only"
    assert "只有识别出的文字" in p.statement
    fig = image("fig", ocr="一些字", figure=True)
    p, rd = run_plan(Placement(turns=(("m1", ("fig",)),)), [(fig, png())], sees=False)
    assert p.needs_eyes and p.parts == {} and rd.files_read == []
    pdf = doc("d", ["正文"], figures=(2,))
    p, rd = run_plan(Placement(turns=(("m1", ("d",)),)), [(pdf, b"%PDF")], sees=False)
    assert p.needs_eyes and rd.files_read == []


def test_deleted_and_folded_attachments_are_named_not_sent():
    gone = doc("g", ["x"], deleted=True)
    folded = doc("f", ["y"])
    p, _ = run_plan(Placement(turns=(("m1", ("g",)),), folded=("f",)),
                    [(gone, None), (folded, None)], sees=False)
    assert p.parts == {} and "g" not in p.reads
    assert "已删除或已清理，内容不在了" in p.statement
    assert "【f.pdf · source_id=f · 在更早（已摘要）的对话里给过" in p.statement


def test_a_text_first_pdf_says_what_its_text_is_and_how_to_read_a_page():
    a = doc("a", ["x"])
    a = replace(a, source=replace(a.source, text_first=True, imprecise_pages=(3, 4, 7)))
    p, _ = run_plan(Placement(turns=(("m", ("a",)),)), [(a, None)], sees=False)
    assert "第 3–4、7 页尤其" in p.statement and "read_source(precise=true)" in p.statement
    assert "不要凭记忆" in p.statement


def test_a_section_that_is_only_a_picture_is_no_unreadable_section():
    a = doc("a", ["x"])
    sec = replace(a.sections[0], text="", page_from=2, page_to=2, read_by="none")
    a = replace(a, sections=(sec,), source=replace(a.source, pages=2, text_first=True, image_pages=(2,)))
    assert "第 2 页有图片或绘制的公式" in full_text(a) and "读不出字" not in full_text(a)
    assert "第 2 页有图片或绘制的公式" in toc_text(a)


def test_a_page_with_a_picture_is_left_out_of_the_prompt_down_to_its_opening_words():
    a = doc("a", ["x"])
    body = "〔p.1〕\nclean page text\n\n〔p.2〕\nDefinitions\n- Density\n" + "x " * 80 + "the density of a network"
    sec = replace(a.sections[0], text=body, page_from=1, page_to=2)
    a = replace(a, sections=(sec,),
                source=replace(a.source, pages=2, text_first=True, image_pages=(2,),
                               imprecise_pages=(2,)))
    assert "clean page text" in full_text(a) and "Definitions - Density" in full_text(a)
    assert "the density of a network" not in full_text(a)
    p, _ = run_plan(Placement(turns=(("m", ("a",)),)), [(a, None)], sees=False)
    assert "第 2 页有图片或绘制的公式" in p.statement and "其余页已给出" in p.statement
    assert "整份已给出" not in p.statement and "尤其" not in p.statement
    assert p.reads["a"].unseen_pages == (2,) and p.reads["a"].pages_read == 1
    assert "the density of a network" not in p.parts["m"][0].text


def test_unreadable_pages_are_stated():
    a = Attachment(src("u", pages=10, unreadable=(3, 4, 5, 9)), ("ask",), False,
                   doc("u", ["正文"]).sections, {})
    p, _ = run_plan(Placement(turns=(("m1", ("u",)),)), [(a, None)], sees=False)
    assert "第 3–5、9 页没有读到内容" in p.statement
    assert p.reads["u"].pages_read == 6 and p.reads["u"].pages_total == 10


def test_ensure_usable():
    ensure_usable([doc("a", ["x"]), doc("b", ["x"], status="partial")])
    with pytest.raises(AttachmentReading):
        ensure_usable([doc("a", ["x"], status="reading")])
    with pytest.raises(AttachmentFailed):
        ensure_usable([doc("a", ["x"], status="failed")])
    with pytest.raises(AttachmentGone):
        ensure_usable([doc("a", ["x"], deleted=True)])


def test_card_expiry():
    assert attachment_out(doc("a", ["x"])).expires_at == T0 + timedelta(days=30)
    assert attachment_out(doc("a", ["x"], uses=("ask", "learn"))).expires_at is None
    a = doc("a", ["x"])
    linked = Attachment(a.source, a.uses, True, a.sections, a.reads)
    assert attachment_out(linked).expires_at is None
    used = replace(a, last_used=T0 + timedelta(days=5))     # newest use, as the cleanup counts
    assert attachment_out(used).expires_at == T0 + timedelta(days=35)
    assert attachment_out(replace(a, uses=())).expires_at is None      # never cleaned up
    gone = attachment_out(doc("a", ["x"], deleted=True))
    assert gone.deleted and gone.expires_at is None and gone.title == "a.pdf"


def test_reader_scopes_by_user():
    rd = reader((doc("a", ["x"]), None))
    with pytest.raises(AttachmentGone):
        rd.load("u2", ["a"])
    with pytest.raises(AttachmentGone):
        rd.file("u1", "a")                                 # no original stored


def test_over_the_limit_older_messages_give_way_earliest_first():
    old = doc("old", ["旧" * 3000], figures=(1,))
    mid = doc("mid", ["中" * 3000])
    cur = doc("cur", ["新" * 3000])
    pl = Placement(turns=(("m1", ("old",)), ("m2", ("mid",)), ("m3", ("cur",))))
    items = [(old, b"%PDF"), (mid, None), (cur, None)]
    p, _ = run_plan(pl, items, sees=True, budget_tokens=100_000, limit_tokens=7000,
                    render=fake_render)
    assert {k: v.mode for k, v in p.reads.items()} == {"old": "toc", "mid": "full",
                                                         "cur": "full"}
    assert p.reads["old"].unseen_pages == (1,) and [x.kind for x in p.parts["m1"]] == ["doc"]
    assert "第 1 页是图，之前那一轮已经看过，见当时的回答" in p.statement
    with pytest.raises(ContextOverBudget):
        run_plan(pl, items, sees=True, budget_tokens=100_000, limit_tokens=2000,
                 render=fake_render)


def test_ocr_text_counts_in_the_budget():
    a = doc("a", ["字" * 1000])
    ocr = image("pic", ocr="识" * 1500)
    pl = Placement(turns=(("m1", ("pic",)), ("m2", ("a",))))
    p, _ = run_plan(pl, [(ocr, png()), (a, None)], sees=False, budget_tokens=2000)
    assert p.reads["pic"].mode == "text_only"
    assert p.reads["a"].mode == "toc"                    # 1000 + ~1500 of OCR text > 2000
    p, _ = run_plan(pl, [(ocr, png()), (a, None)], sees=False, budget_tokens=3000)
    assert p.reads["a"].mode == "full"
    # the current message's OCR text alone over the limit fails closed
    with pytest.raises(ContextOverBudget):
        run_plan(Placement(turns=(("m1", ("pic",)),)), [(ocr, png())], sees=False,
                 limit_tokens=1000)


def test_older_figure_pages_over_the_cap_were_never_seen():
    old = doc("old", ["正文"], figures=tuple(range(1, 13)))    # 12 figure pages: 11–12 capped
    pics = [image(f"p{i}") for i in range(MAX_FIGURE_PAGES + 1)]  # the last one is over the cap
    pl = Placement(turns=(("m1", ("old",)), ("m2", tuple(x.source_id for x in pics)),
                          ("m3", ())))
    p, rd = run_plan(pl, [(old, b"%PDF"), *((x, png()) for x in pics)], sees=False)
    assert not p.needs_eyes and rd.files_read == []
    assert ("第 1–10 页是图，之前那一轮已经看过，见当时的回答 · 第 11–12 页没有读到内容"
            in p.statement)
    assert "【p0.png · source_id=p0 · 图片 · 之前那一轮已经看过，见当时的回答】" in p.statement
    assert ("【p10.png · source_id=p10 · 图片 · 这一轮没给出原图，也没有识别出的文字】"
            in p.statement)


def test_card_shows_unreadable_pages_and_characters():
    secs = (sec("u", 0, "〔p.1〕一二三 四\n五"), sec("u", 1, "六"),
            sec("u", 2, "不算", read_by="none"))
    a = Attachment(src("u", pages=10, unreadable=(3, 4)), ("ask",), False, secs, {})
    card = attachment_out(reader((a, None)).load("u1", ["u"], content=False)[0])
    assert card.unreadable_pages == (3, 4) and card.chars == 6
    assert attachment_out(image("pic", ocr="字")).chars is None
    assert attachment_out(doc("g", ["字"], deleted=True)).chars is None
    assert attachment_out(Attachment(src("r", status="reading"), ("ask",))).chars is None


# ---- follow-up B: which attachment 「这份 / 刚才那份」 means ----

def test_order_numbers_by_first_message_and_marks_the_last_attached_or_mentioned():
    from evowiki.assistant.attachments import order
    titles = {"lec": "Eigenvalues (lecture notes)", "big": "Understanding Linear Algebra",
              "fig": "figure"}
    o = order([(("lec",), "这份讲义讲了什么"), (("big",), "Example 4.2.4"), (("fig",), "这道题"),
               ((), "把这份讲义收进资料")], titles)
    assert dict(o.origin) == {"lec": 1, "big": 2, "fig": 3} and o.recent == "fig"
    o = order([(("lec",), ""), (("big",), ""), ((), "回到 eigenvalues (lecture notes) 那份")],
              titles)
    assert o.recent == "lec"                               # named by its title: mentioned
    o = order([(("lec", "big"), "")], {"lec": "x", "big": "b"})   # one-character titles never match
    assert o.recent == "big" and o.origin == {"lec": 1, "big": 1}
    assert order([(("gone",), "")], {}).recent is None     # a deleted one is never "recent"


def test_statement_lists_in_upload_order_with_message_kind_and_the_recent_mark():
    from evowiki.assistant.attachments import Order
    lec, big, fig = doc("lec", ["讲义"], title="Eigenvalues (lecture notes)"), doc("big", ["书"]), (
        image("fig", ocr="sin x"))
    pl = Placement(turns=(("m2", ("big",)), ("m3", ("fig",))), folded=("lec",))
    p, _ = run_plan(pl, [(lec, None), (big, None), (fig, png())], sees=False,
                    budget_tokens=10_000)
    lines = p.statement.splitlines()[1:]
    assert [ln.split("【")[0] for ln in lines] == [          # placement order without an Order
        "#1（PDF）", "#2（图片）", "#3（PDF）"]
    ordered = Order(origin={"lec": 1, "big": 2, "fig": 3}, recent="fig")
    p = plan(pl, {a.source_id: a for a in (lec, big, fig)}, sees=False, budget_tokens=10_000,
             limit_tokens=100_000, user_id="u1", reader=reader((lec, None), (big, None),
                                                               (fig, png())),
             render=no_render, ordered=ordered)
    lines = p.statement.splitlines()[1:]
    assert lines[0].startswith("#1（随第 1 条消息，PDF）【Eigenvalues (lecture notes) · source_id=lec")
    assert lines[1].startswith("#2（随第 2 条消息，PDF）【big.pdf · source_id=big")
    assert lines[2].startswith("#3（随第 3 条消息，图片，最近的）【fig.png · source_id=fig")
    assert p.statement.count("最近的") == 1 and "按上传顺序编号" in p.statement.splitlines()[0]
