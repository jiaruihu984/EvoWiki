"""活动记录 hides 撤销 of a rule that moved on (PreferenceExecutor.superseded, bot.actions.can_undo)."""

from types import SimpleNamespace

from evowiki.bot.actions import can_undo
from evowiki.bot.executors_prefs import PreferenceExecutor
from evowiki.wikiapp import teachspec as tspec


def _row(action_id, item_id, text):
    return SimpleNamespace(action_id=action_id, kind="stated_rule", status="executed", level="auto_undo",
                           undo={"mode": "stated", "item_id": item_id, "text": text})


def _executor(monkeypatch, items):
    ex = PreferenceExecutor.__new__(PreferenceExecutor)
    ex._repo = SimpleNamespace(load=lambda user_id: SimpleNamespace(state=None))
    monkeypatch.setattr(tspec, "effective_spec", lambda state: SimpleNamespace(
        items=tuple(SimpleNamespace(item_id=i, text=t) for i, t in items.items())))
    return ex


def test_only_the_rows_undo_would_refuse(monkeypatch):
    same, edited, deleted = _row("a", "r1", "先给例子"), _row("b", "r2", "多举足球"), _row("c", "r3", "别的")
    ex = _executor(monkeypatch, {"r1": "先给例子", "r2": "多举足球，也可以举篮球"})
    moved = ex.superseded("u", [same, edited, deleted])
    assert moved == {"b", "c"}
    assert can_undo(same, moved) and not can_undo(edited, moved) and not can_undo(deleted, moved)


def test_a_row_without_undo_data_needs_no_read(monkeypatch):
    ex = PreferenceExecutor.__new__(PreferenceExecutor)
    ex._repo = None
    assert ex.superseded("u", [SimpleNamespace(action_id="a", undo={})]) == set()
