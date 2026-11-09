"""「从 X 开始」 on a confirmed goal's 学习路径 (`wikiapp.map_edits`, the same code as the card). His edits of the
path from the chat are `edit_path_blocks` in tools/actions.py: a ledger row like every other change."""

from collections.abc import Callable
from datetime import datetime

from evowiki.wikiapp.map_edits import edit_map, placed
from evowiki.wikiapp.repo import Repo


def put_first(repo: Repo, user_id: str, map_id: str, name: str, now: datetime,
              new_id: Callable[[str], str]) -> None:
    """「从 X 开始」: X is the first block of the path, so the next block entered is X."""
    m = repo.load(user_id).state.maps.get(map_id)
    if m is None:
        raise LookupError(f"没有这个目标：{map_id}")
    edit_map(repo, user_id, map_id, [placed(m, name, 1)], now=now, new_id=new_id)
