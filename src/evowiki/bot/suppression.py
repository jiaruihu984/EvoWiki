"""Learned suppression (spec 2026-10-14 §3): explicit reactions per dedup key."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

# 开始 / 不感兴趣, and 完成 on a card that was started. `later` / `saved` are gone (user ruling
# 2026-10-14: a card is started, declined, or ignored until it expires).
Reaction = Literal["accepted", "not_interested", "done"]


@dataclass(frozen=True)
class Counters:
    accepted: int = 0
    not_interested: int = 0
    later: int = 0
    last_not_interested_at: datetime | None = None


def react(c: Counters, reaction: Reaction, now: datetime) -> Counters:
    """Counters after an explicit user reaction. Only 不感兴趣 suppresses; 接受 resets it; 完成
    changes nothing."""
    if reaction == "accepted":
        return replace(c, accepted=c.accepted + 1, not_interested=0)
    if reaction == "not_interested":
        return replace(c, not_interested=c.not_interested + 1, last_not_interested_at=now)
    return c


def suppressed_until(c: Counters, base: timedelta) -> datetime | None:
    """Each 不感兴趣 doubles the cooldown: last time + base * 2**count; None without any."""
    if c.not_interested == 0 or c.last_not_interested_at is None:
        return None
    return c.last_not_interested_at + base * 2 ** c.not_interested
