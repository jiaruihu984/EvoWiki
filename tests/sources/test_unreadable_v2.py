"""Unreadable-page judge v2: strong / weak signals, formula-or-table pages, image pages."""

import json
from dataclasses import asdict
from pathlib import Path

from evowiki.sources import unreadable as u
from evowiki.sources.parse_pdf import parse_pdf
from evowiki.sources.parsed import PageText
from evowiki.sources.unreadable import COMMON_HANZI, judge_all, judge_page, wants_ocr

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "sources"

# A plain lecture page: long lines, a heading, a page number, some digits and a little English.
LECTURE = """第三章 间隔重复
3.1 为什么要把复习分散开
间隔重复是一种把复习安排在遗忘之前的方法。每次复习之后，下一次复习的间隔会变长，
这样同样的时间可以记住更多的内容。它常常和提取练习一起使用，效果比集中复习好得多。
艾宾浩斯在 1885 年做过一组实验：学完一张无意义音节表以后，第 1 天、第 2 天、第 6 天
各复习一次，比同一天连续复习六次记得更牢。后来的研究把这种现象叫作间隔效应
（spacing effect），在词汇、医学知识和编程练习里都得到了验证。
课后请用自己的话写出间隔效应的定义，并举一个生活中的例子。
第 12 页"""

ENGLISH = (
    "Retrieval practice means pulling a fact out of memory instead of re-reading it. "
    "A small quiz after each section is a simple way to do it, and it works for adults too."
)

RARE = [chr(c) for c in range(0x4E00, 0x9FA6) if chr(c) not in COMMON_HANZI][:400]


def _page(text: str, page: int = 1, **kw) -> PageText:
    return PageText(page=page, text=text, read_by="text", **kw)


def _judge(text: str, *, fonts: bool = False, area: float = 0.0) -> PageText:
    return judge_page(_page(text), fonts, area)


def _nonspace(text: str) -> int:
    return sum(1 for ch in text if not ch.isspace())


# ---- thresholds and the word list ----------------------------------------------------------

def test_thresholds() -> None:
    assert (u.REPLACEMENT_RATIO, u.PRIVATE_USE_RATIO, u.CID_MIN, u.CONTROL_RATIO) == (
        0.02, 0.05, 3, 0.01)
    assert (u.RARE_HANZI_RATIO, u.RARE_HANZI_MIN, u.RUN_LENGTH, u.RUN_RATIO) == (
        0.3, 20, 6, 0.1)
    assert u.WEAK_POINTS == 2
    assert (u.MATH_RATIO, u.SHORT_LINE_RATIO, u.SHORT_LINE_CHARS) == (0.03, 0.4, 12)
    assert u.IMAGE_AREA == 0.3
    # Phase 1 values stay.
    assert (u.MIN_CHARS_PER_PAGE, u.MAX_GARBLE_RATIO, u.MAX_FRAGMENT_RATIO) == (40, 0.3, 0.7)


def test_common_hanzi_list_has_3500_simplified_chars() -> None:
    assert len(COMMON_HANZI) == 3500
    assert all("一" <= ch <= "鿿" for ch in COMMON_HANZI)
    assert {"的", "学", "习", "复", "间", "隔"} <= COMMON_HANZI


# ---- pages that must stay text ----------------------------------------------------------------

def test_lecture_page_is_plain_text() -> None:
    out = _judge(LECTURE)
    assert (out.read_by, out.reason, out.signals, out.figure) == ("text", None, (), False)
    assert not wants_ocr(out)


def test_english_page_is_plain_text() -> None:
    out = _judge(ENGLISH)
    assert (out.read_by, out.reason, out.signals) == ("text", None, ())


# ---- strong signals: any one sends the page to OCR --------------------------------------------

def test_replacement_chars_at_two_percent() -> None:
    base = LECTURE * 2
    n = _nonspace(base)
    hit = _judge(base + "\ufffd" * (n // 45))            # ~2.2 %
    assert (hit.read_by, hit.reason) == ("none", "garbled")
    assert "replacement_chars" in hit.signals
    miss = _judge(base + "\ufffd" * (n // 60))           # ~1.6 %
    assert miss.read_by == "text" and "replacement_chars" not in miss.signals


def test_private_use_at_five_percent() -> None:
    base = LECTURE * 2
    n = _nonspace(base)
    hit = _judge(base + "\ue000\uf8ff" * (n // 30))      # ~6.0 %
    assert (hit.read_by, hit.reason) == ("none", "garbled")
    assert "private_use" in hit.signals
    miss = _judge(base + "\ue123" * (n // 25))           # ~3.8 %
    assert miss.read_by == "text"


def test_three_cid_codes() -> None:
    hit = _judge(LECTURE + " (cid:12) 和 (cid:7) 以及 (cid:300)")
    assert (hit.read_by, hit.reason) == ("none", "garbled")
    assert "cid_codes" in hit.signals
    assert _judge(LECTURE + " (cid:12) 和 (cid:7)").read_by == "text"


def test_control_chars_at_one_percent() -> None:
    base = LECTURE * 2
    n = _nonspace(base)
    hit = _judge(base + "\x07\x01" * (n // 150))         # ~1.2 %
    assert (hit.read_by, hit.reason) == ("none", "garbled")
    assert "control_chars" in hit.signals
    # tabs / newlines / form feeds are layout, not garble
    assert _judge(base + "\t\x0c" * 50).read_by == "text"


def test_font_without_tounicode_is_a_hint() -> None:
    # pdfium falls back to the raw codes: the text may look like clean letters. Alone the
    # font flag only asks for OCR; the text layer stays as the fallback (controller ruling:
    # it fired on 124 born-digital negatives as a strong signal).
    out = _judge("ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 4, fonts=True)
    assert (out.read_by, out.reason) == ("text", None)
    assert "no_unicode_map" in out.signals
    assert wants_ocr(out)
    # an empty page stays "no_text"
    empty = _judge("", fonts=True)
    assert (empty.read_by, empty.reason) == ("none", "no_text")


def test_font_without_tounicode_plus_a_strong_signal_is_unreadable() -> None:
    out = _judge(LECTURE + " (cid:1) (cid:2) (cid:3)", fonts=True)
    assert (out.read_by, out.reason) == ("none", "garbled")
    assert {"no_unicode_map", "cid_codes"} <= set(out.signals)


def test_font_without_tounicode_plus_two_weak_points_is_unreadable() -> None:
    rare = _rare_text(200)
    out = _judge(rare + "\n" + "".join(ch * 8 for ch in RARE[:8]), fonts=True)
    assert (out.read_by, out.reason) == ("none", "garbled")
    assert {"no_unicode_map", "rare_hanzi", "char_runs"} <= set(out.signals)


# ---- weak signals: one point each, two points send the page ----------------------------------

def _rare_text(n: int) -> str:
    return "".join(RARE[i % len(RARE)] for i in range(n))


def test_rare_hanzi_alone_is_one_point_only() -> None:
    text = LECTURE + _rare_text(200)
    out = _judge(text)
    assert "rare_hanzi" in out.signals
    assert out.read_by == "text"


def test_rare_hanzi_needs_twenty_hanzi() -> None:
    out = _judge("ABCDEFGHIJ abcdefghij KLMNOPQRST klmnopqrst " + _rare_text(19))
    assert "rare_hanzi" not in out.signals


def test_garbled_page_with_plenty_of_chars_is_flagged() -> None:
    # The kind phase 1 missed (recall 0.62): a wrong cmap turns a lecture into rare hanzi
    # with stuttering runs; 400+ characters, no U+FFFD, no private use, no fragments.
    rare = _rare_text(300)
    runs = "".join(ch * 8 for ch in RARE[:8])
    text = "\n".join(rare[i:i + 40] + runs[i // 40 * 8:(i // 40 + 1) * 8 * 2]
                     for i in range(0, 300, 40))
    assert _nonspace(text) > 300
    out = _judge(text)
    assert (out.read_by, out.reason) == ("none", "garbled")
    assert {"rare_hanzi", "char_runs"} <= set(out.signals)


def test_char_runs_alone_is_one_point_only() -> None:
    out = _judge(LECTURE + "\n" + "啊" * 80)
    assert "char_runs" in out.signals
    assert out.read_by == "text"


def test_dot_leaders_are_not_char_runs() -> None:
    toc = "\n".join(f"第{i}章 学习方法和复习安排 ........................ {i * 7}"
                    for i in range(1, 12))
    out = _judge(toc)
    assert "char_runs" not in out.signals


def test_runs_plus_fragments_is_flagged() -> None:
    # 78 one-letter tokens + 20 words + 1 run token: fragments ~0.79 (weak, not "sparse")
    frag = " ".join(list("xyzqwertyuiop") * 6)
    out = _judge(frag + " " + "practice " * 20 + "ÿ" * 60)
    assert {"fragments", "char_runs"} <= set(out.signals)
    assert (out.read_by, out.reason) == ("none", "garbled")


def test_few_chars_alone_still_goes() -> None:
    out = _judge("学习方法导论" * 5, area=0.2)
    assert (out.read_by, out.reason) == ("none", "no_text")
    assert "few_chars" in out.signals


# ---- formula / table pages: try OCR, keep the text layer as fallback -------------------------

def test_formula_page() -> None:
    text = LECTURE + "\n设 α + β = π，则 ∑ aᵢ ≤ ∫ f(x) dx ≤ √n · ∞，并且 ∂f/∂x = ∇g，Δ ≥ ±σ × λ ÷ θ"
    out = _judge(text)
    assert (out.read_by, out.reason) == ("text", None)
    assert "formula_or_table" in out.signals
    assert wants_ocr(out)


def test_table_page_of_short_numeric_lines() -> None:
    rows = "\n".join(f"{i}\n{i * 3.5:.1f}\n{i * 12}%" for i in range(1, 9))
    text = "表 2 各组复习间隔与正确率（单位：天、百分比）对照，数据来自第二次实验。\n" + rows
    out = _judge(text)
    assert out.read_by == "text"
    assert "formula_or_table" in out.signals
    assert wants_ocr(out)


def test_lecture_with_a_few_numbers_is_not_a_table() -> None:
    assert "formula_or_table" not in _judge(LECTURE).signals


# ---- image pages: candidate figure, OCR decides ----------------------------------------------

def test_image_area_threshold() -> None:
    hit = _judge(LECTURE, area=0.3)
    assert hit.read_by == "text" and "image_page" in hit.signals
    assert hit.figure is False          # the vision step decides figure after OCR
    assert wants_ocr(hit)
    miss = _judge(LECTURE, area=0.29)
    assert "image_page" not in miss.signals and not wants_ocr(miss)


def test_unreadable_page_wants_ocr() -> None:
    assert wants_ocr(_judge(""))


# ---- judge_all reads the parser's fields ------------------------------------------------------

def test_judge_all_uses_page_fields() -> None:
    pages = [_page(LECTURE, 1),
             _page("ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 4, 2, fonts_without_tounicode=True),
             _page(LECTURE, 3, image_area=0.5)]
    out = judge_all(pages)
    assert [(p.page, p.read_by, p.reason) for p in out] == [
        (1, "text", None), (2, "text", None), (3, "text", None)]
    assert out[1].signals == ("no_unicode_map",)
    assert out[2].signals == ("image_page",)


def test_judge_is_idempotent() -> None:
    once = judge_all([_page(LECTURE, 1, image_area=0.5)])
    assert judge_all(once) == once


def test_signals_survive_a_json_round_trip() -> None:
    page = judge_page(_page(LECTURE), False, 0.6)
    back = PageText(**json.loads(json.dumps(asdict(page))))
    assert back == page and isinstance(back.signals, tuple)


# ---- parse_pdf fills the fields from pdfium ---------------------------------------------------

def test_parse_pdf_flags_fonts_without_tounicode() -> None:
    doc = parse_pdf((FIX / "no_tounicode.pdf").read_bytes(), title_hint="x")
    (page,) = doc.pages
    assert page.fonts_without_tounicode is True
    assert len(page.text.strip()) > 200              # pdfium still returns letters
    (judged,) = judge_all(doc.pages)
    assert (judged.read_by, judged.reason) == ("text", None)
    assert judged.signals == ("no_unicode_map",) and wants_ocr(judged)


def test_parse_pdf_normal_fonts_are_mapped() -> None:
    doc = parse_pdf((FIX / "outline.pdf").read_bytes(), title_hint="x")
    assert not any(p.fonts_without_tounicode for p in doc.pages)
    assert all(p.image_area == 0.0 for p in doc.pages)


def test_parse_pdf_image_area() -> None:
    doc = parse_pdf((FIX / "figure_page.pdf").read_bytes(), title_hint="x")
    small, big = doc.pages
    assert abs(small.image_area - 0.1) < 0.01
    assert abs(big.image_area - 0.5) < 0.01
    judged = judge_all(doc.pages)
    assert [p.signals for p in judged] == [(), ("image_page",)]
    assert [p.read_by for p in judged] == ["text", "text"]


def test_parse_pdf_full_page_scan() -> None:
    doc = parse_pdf((FIX / "mixed_scan.pdf").read_bytes(), title_hint="x")
    assert doc.pages[4].image_area > 0.99 and doc.pages[0].image_area == 0.0
    judged = judge_all(doc.pages)
    assert (judged[4].read_by, judged[4].reason) == ("none", "no_text")
    assert "image_page" in judged[4].signals


def test_parse_pdf_image_inside_scaled_forms() -> None:
    # Pictures placed through Form XObjects (PPT / Word / InDesign exports): bounds come in
    # the form's space and must be mapped through every enclosing form's matrix.
    doc = parse_pdf((FIX / "form_image.pdf").read_bytes(), title_hint="x")
    expected = 400 * 400 / (595.2756 * 841.8898)
    assert [round(p.image_area, 3) for p in doc.pages] == [round(expected, 3)] * 2
    assert all("image_page" in p.signals for p in judge_all(doc.pages))


def test_unicode_map_error_code_is_not_a_missing_map(monkeypatch) -> None:
    import pypdfium2.raw as pdfium_c
    monkeypatch.setattr(pdfium_c, "FPDFText_HasUnicodeMapError", lambda textpage, i: -1)
    doc = parse_pdf((FIX / "outline.pdf").read_bytes(), title_hint="x")
    assert not any(p.fonts_without_tounicode for p in doc.pages)
