"""The Bot reads a path in the order the user sees (the user's edits
replayed: wikiapp.path_order), the same order the plugin shows and 「下一页」 follows."""

from datetime import UTC, datetime
from types import SimpleNamespace

from evowiki.bot.facts import _goals, page_label
from evowiki.goals.path_edit import MoveEdit, RemoveEdit
from evowiki.wikiapp.events import PathRecord
from tests.wikiapp.test_k1_state import lp

T = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)


def _state(page_ids, edits=(), done=()):
    rec = PathRecord(path=lp(3), page_ids=page_ids, created_at=T, edits=edits)
    pages = {p: SimpleNamespace(title=f"T{p[-1]}", done=p in done) for p in page_ids if p}
    return SimpleNamespace(paths={"path-k": rec}, pages=pages, opened_at={}, goal_settings={})


MOVE_3_FIRST = (MoveEdit(page_key="path-k-p3", to_index=0),)


def test_goal_next_page_follows_the_edited_order():
    plain = _state(("pg_1", "pg_2", "pg_3"), done=("pg_1",))
    assert _goals(plain)[0].next_page_id == "pg_2"
    moved = _state(("pg_1", "pg_2", "pg_3"), MOVE_3_FIRST, done=("pg_1",))
    assert _goals(moved)[0].next_page_id == "pg_3"
    # the edited order's next page is not started yet: no next wiki page, still unfinished
    unstarted = _state(("pg_1", "pg_2", None), MOVE_3_FIRST)
    (g,) = _goals(unstarted)
    assert g.next_page_id is None


def test_goal_done_when_every_page_left_in_the_edited_order_is_done():
    st = _state(("pg_1", "pg_2", None), (RemoveEdit(page_key="path-k-p3"),),
                done=("pg_1", "pg_2"))
    assert _goals(st) == ()


def test_page_label_is_the_title_without_a_position_suffix():
    plain = _state(("pg_1", "pg_2", "pg_3"))
    moved = _state(("pg_1", "pg_2", "pg_3"), MOVE_3_FIRST)
    removed = _state(("pg_1", "pg_2", "pg_3"), (RemoveEdit(page_key="path-k-p2"),))
    for st in (plain, moved, removed):
        assert [page_label(st, p) for p in ("pg_1", "pg_2", "pg_3")] == ["T1", "T2", "T3"]
        assert "（" not in page_label(st, "pg_3")
