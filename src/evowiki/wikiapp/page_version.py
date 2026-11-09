"""Page fingerprint: an action proposed against a page version must not overwrite a newer one."""

from evowiki.agent.effects import payload_hash
from evowiki.wikiapp.events import PageRecord

ABSENT = "absent"


def page_fingerprint(rec: PageRecord | None) -> str:
    if rec is None or rec.page is None:
        return ABSENT
    return payload_hash(rec.page.model_dump(mode="json"))


def page_text_fingerprint(rec: PageRecord | None) -> str:
    """What the page says: its sections' headings and texts. Opening a page, folds and K marks save the record without
    changing this, so what was judged on the text (goals/page_fit.py) still holds after them."""
    page = rec.page if rec is not None else None
    if page is None:
        return ABSENT
    return payload_hash([[s.id, s.heading, t.body if (t := page.texts.get(s.id)) is not None else ""]
                         for s in page.outline.sections])


def text_fingerprint(heading: str, body: str) -> str:
    return payload_hash({"heading": heading, "body": body})


def section_fingerprint(rec: PageRecord | None, section_id: str | None) -> str:
    """One section's heading and text: a change made for that section can still go onto the page
    after other sections changed, as long as this one is the same (re-anchoring)."""
    page = rec.page if rec is not None else None
    sec = next((s for s in page.outline.sections if s.id == section_id), None) if page else None
    if page is None or sec is None:
        return ABSENT
    text = page.texts.get(sec.id)
    return text_fingerprint(sec.heading, text.body if text is not None else "")
