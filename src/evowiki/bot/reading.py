"""Reading time (B5 spec §1.6): the plugin reports active reading seconds per wiki page in batches;
they are summed per user, page and local day (the Bot settings' time zone). A resent batch is
not counted twice; entries for pages the user does not have, or outside the accepted time
window, are dropped and counted (never silently). Missing data means unknown, never zero: the
weekly review reads a week as known only when the first report is not later than its start
(`facts.load_goal_week`)."""

from datetime import datetime, timedelta

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from evowiki.localday import local_date
from evowiki.store import bot as store_bot
from evowiki.store import bot_goals
from evowiki.wikiapp.repo import load_in

MAX_SECONDS = 600                     # one entry: at most 10 minutes (one heartbeat + slack)
MAX_AGE = timedelta(days=2)           # a queued batch older than this is not counted
MAX_AHEAD = timedelta(minutes=5)      # clock skew allowed for `at`


class ReadingEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page_id: str = Field(min_length=1, max_length=200)
    seconds: int = Field(ge=1, le=MAX_SECONDS)
    at: AwareDatetime                  # when the counted stretch ended


class ReadingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=8, max_length=80)
    entries: list[ReadingEntry] = Field(min_length=1, max_length=200)


class ReadingOut(BaseModel):
    accepted: int                      # entries counted
    dropped: int                       # unknown page or outside the time window
    duplicate: bool                    # the batch had been counted already: nothing added


def record_reading(pool, user_id: str, body: ReadingIn, *, now: datetime) -> ReadingOut:
    with pool.connection() as conn, conn.transaction():
        pages = load_in(conn, user_id).state.pages
        tz = store_bot.get_settings(conn, user_id).timezone
        sums: dict[tuple[str, object], int] = {}
        dropped = 0
        for e in body.entries:
            if e.page_id not in pages or e.at > now + MAX_AHEAD or e.at < now - MAX_AGE:
                dropped += 1
                continue
            key = (e.page_id, local_date(e.at, tz))
            sums[key] = sums.get(key, 0) + e.seconds
        rows = [(p, d, s) for (p, d), s in sorted(sums.items(), key=lambda kv: (kv[0][0], str(kv[0][1])))]
        added = bot_goals.add_reading(conn, user_id, body.batch_id, rows, now)
    if not added:
        return ReadingOut(accepted=0, dropped=0, duplicate=True)
    return ReadingOut(accepted=len(body.entries) - dropped, dropped=dropped, duplicate=False)
