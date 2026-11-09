"""The user's local calendar day. (Not in `timeutil`: the simulator imports that file and its hash is frozen.)"""

from datetime import date, datetime
from zoneinfo import ZoneInfo


def local_date(now: datetime, tz: str) -> date:
    """The calendar day of `now` in the IANA zone `tz`."""
    return now.astimezone(ZoneInfo(tz)).date()
