"""The profile over the wiki: the note as the vault gets it, his edits of it, facts from the chat,
and `context` (user id, purpose) -> the block the gateway appends to that call's system prompt.

Each thing has one store (`profile.note`'s module doc). 怎么学比较好 is the teaching spec's own items:
deleting a line there revokes that item, a line he types there adds one (origin user_ui,
`wikiapp.guidelines`), so the note and what the writer follows never differ. 已经会的 is K: a line he
types there is a self-report in K (`profile.known`); one that matches no point comes back in the
note with why, and is never stored; a line he deletes there takes back what he said (`known.retraction`),
one ledger row (`KnownLog`) undoes it."""

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from threading import Lock
from typing import Any, Literal, Protocol

from evowiki.profile import known as kn
from evowiki.profile.context import context_for
from evowiki.profile.derive import derive_view
from evowiki.profile.note import (
    HOW,
    KNOWN,
    Edit,
    Profile,
    add_fact,
    merge_edit,
    note_version,
    owned_lines,
    render_note,
    shown,
    with_sent,
)
from evowiki.profile.store import ProfileStore
from evowiki.wikiapp.guidelines import add_guideline, remove_guideline, said_rules
from evowiki.wikiapp.kchange import k_changed
from evowiki.wikiapp.kwrite import apply_in_memory, observation_events
from evowiki.wikiapp.page_run_store import PageRunRow
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiConflict
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import GuidelineIn

# the gateway's view: (user id, purpose) -> the profile block for that call ("" = none)
ProfileSource = Callable[[str, str], str]

SNAPSHOTS_MAX = 256            # users whose snapshot is kept
COMMIT_TRIES = 3               # K writes of 已经会的 lines tried on a conflict


@dataclass(frozen=True)
class NoteCopy:
    """The note as the vault gets it. `kept`: rules and owned lines a save could not retract (his note lacks them)."""

    text: str
    version: str
    kept: tuple[str, ...] = ()
    retracted: "Retracted | None" = None
    locale: Literal["zh", "en"] = "zh"
    format_version: Literal[2] = 2


@dataclass(frozen=True)
class Retracted:
    """What a save took back from K, and the ledger row that undoes it (None: no ledger here)."""

    lines: tuple[str, ...]
    action_id: str | None


class KnownLog(Protocol):
    """The ledger's row of a retraction (`bot.executors_profile.KnownRetractLog`)."""

    def record_in(self, conn: Any, user_id: str, key: str, retraction: kn.Retraction) -> None: ...
    def action_id(self, key: str) -> str | None: ...


class HeadRepo(Repo, Protocol):
    def head(self, user_id: str) -> tuple[int, int, int]: ...
    # (wiki seq, aspect seq, infer version) a `load` now would report, without replaying


class ProfileService:
    def __init__(self, repo: HeadRepo, store: ProfileStore, *, clock: Callable[[], datetime],
                 new_id: Callable[[str], str],
                 runs: Callable[[str, WikiState], Mapping[str, PageRunRow]] = lambda _u, _s: {},
                 known_log: KnownLog | None = None):
        """`runs(user_id, state)`: the pages' latest write runs (what GET /wiki/progress reads).
        `known_log`: where a retraction's undo row goes (set later when the ledger is built after this)."""
        self._repo, self._store, self._clock, self._new_id = repo, store, clock, new_id
        self._runs = runs
        self.known_log = known_log
        self._snaps: OrderedDict[str, Snapshot] = OrderedDict()
        self._lock = Lock()

    def _snapshot(self, user_id: str) -> Snapshot:
        """The user's snapshot, loaded again only when the repo's head moved (LookupError: no
        wiki yet)."""
        head = self._repo.head(user_id)
        with self._lock:
            snap = self._snaps.get(user_id)
            if snap is not None and (snap.wiki_seq, snap.aspect_seq, snap.infer_version) == head:
                self._snaps.move_to_end(user_id)
                return snap
        snap = self._repo.load(user_id)
        with self._lock:
            self._snaps[user_id] = snap
            self._snaps.move_to_end(user_id)
            while len(self._snaps) > SNAPSHOTS_MAX:
                self._snaps.popitem(last=False)
        return snap

    def _derived(self, user_id: str) -> dict[str, tuple[str, ...]]:
        try:
            snap = self._snapshot(user_id)
        except LookupError:              # no wiki yet: only what he said counts
            return {}
        return derive_view(snap, self._clock(), self._runs(user_id, snap.state))[0]

    def sections(self, user_id: str) -> dict[str, tuple[str, ...]]:
        return shown(self._store.get(user_id), self._derived(user_id))

    def context(self, user_id: str, purpose: str) -> str:
        profile = self._store.get(user_id)
        return context_for(shown(profile, self._derived(user_id)), purpose,
                           owned=owned_lines(profile))

    def version(self, user_id: str) -> str:
        """The version of the copy last handed out ("" = none yet)."""
        return self._store.get(user_id).version

    def copy(self, user_id: str) -> NoteCopy:
        """The note for his vault, remembered as the copy he now has."""
        try:
            snap = self._snapshot(user_id)
        except LookupError:
            d, display, locale = {}, {}, "zh"
        else:
            d, english = derive_view(snap, self._clock(), self._runs(user_id, snap.state))
            locale = snap.state.language
            display = english if locale == "en" else {}
        def text(p: Profile) -> str:
            return render_note(p, d, language=locale, display=display)
        p = self._store.update(user_id, lambda p: with_sent(p, d, note_version(text(p))))
        return NoteCopy(text(p), p.version, locale=locale)

    def note(self, user_id: str) -> str:
        return self.copy(user_id).text

    def save(self, user_id: str, text: str, base: str | None = None) -> NoteCopy:
        """His saved note, a copy of version `base`: merged (his edit wins, only a save of the last copy
        retracts), 怎么学比较好 applied to the teaching spec, 已经会的 to K; returns the note as it now is."""
        d = self._derived(user_id)
        got: list[Edit] = []

        def merge(p: Profile) -> Profile:
            got[:] = [merge_edit(p, d, text, base)]
            return got[0].profile

        self._store.update(user_id, merge)
        e = got[0]
        added, removed = e.added.get(HOW, ()), e.removed.get(HOW, ())
        if added or removed:
            self._apply_said(user_id, added, removed)
        took = self._retract_known(user_id, e.removed.get(KNOWN, ()))
        bad = self._apply_known(user_id, e.added.get(KNOWN, ()))
        self._store.update(user_id, lambda p: p.model_copy(update={"unmatched": bad}))
        c = self.copy(user_id)
        return NoteCopy(c.text, c.version, e.kept, took, locale=c.locale)

    def edit(self, user_id: str, text: str, base: str | None = None) -> str:
        return self.save(user_id, text, base).text

    def _apply_known(self, user_id: str, lines: Sequence[str]) -> tuple[str, ...]:
        """His 已经会的 lines as self-reports in K (`profile.known`); returns the lines not
        recorded, each with why (every line when K could not be written)."""
        if not lines:
            return ()
        for _ in range(COMMIT_TRIES):
            try:
                snap = self._repo.load(user_id)
            except LookupError:
                return tuple(kn.unmatched(kn.clean(x), kn.NO_WIKI) for x in lines if kn.clean(x))
            ok, bad = kn.split(snap.state, snap.k, lines)
            if not ok:
                return tuple(bad)
            obs = [o for m in ok for o in kn.observations(m)]
            events = observation_events(obs, ts=self._clock(), new_id=self._new_id,
                                        note_text=lambda o: o.quote or "",
                                        note_label=lambda o: kn.label(o.quote or ""))
            try:
                self._repo.commit(snap, Commit(aspect=events))
                return tuple(bad)
            except WikiConflict:
                continue
        return tuple(kn.unmatched(kn.clean(x), kn.BUSY) for x in lines if kn.clean(x))

    def _retract_known(self, user_id: str, lines: Sequence[str]) -> Retracted | None:
        """The deleted 已经会的 `lines` taken back in K (`known.retraction`), through the one K-change
        path, with the undo row in the same commit. WikiConflict when K kept moving: the save fails, the
        note still lacks the line and the next save does it."""
        if not lines:
            return None
        for attempt in range(1, COMMIT_TRIES + 1):
            try:
                snap = self._repo.load(user_id)
            except LookupError:
                return None
            now = self._clock()
            r = kn.retraction(snap.state, snap.k, self._repo.aspect_events(user_id), lines,
                              ts=now, new_id=self._new_id)
            if r is None:
                return None
            kc = k_changed(snap, apply_in_memory(snap, r.aspect), snap.state.current_page, None,
                           skip=None, now=now, new_id=self._new_id, action="declare")
            key = f"known_retract:{self._new_id('kr')}"
            log = self.known_log
            extra = [] if log is None else [lambda conn, lg=log, r=r, k=key: lg.record_in(conn, user_id, k, r)]
            try:
                self._repo.commit(snap, Commit(aspect=list(r.aspect), wiki=list(kc.wiki), extra=extra))
            except WikiConflict:
                if attempt == COMMIT_TRIES:
                    raise
                continue
            return Retracted(r.lines, None if log is None else log.action_id(key))
        return None

    def _apply_said(self, user_id: str, added: Sequence[str], removed: Sequence[str]) -> None:
        now = self._clock()
        # only the rules the note lists (his own words) can be removed from it
        for i in said_rules(self._repo.load(user_id).state):
            if i.text in removed:
                remove_guideline(self._repo, user_id, i.item_id, now=now, new_id=self._new_id)
        for text in added:
            add_guideline(self._repo, user_id, GuidelineIn(text=text), now=now, new_id=self._new_id)

    def add_fact(self, user_id: str, section: str, text: str, replaces: str = "") -> Profile:
        return self._store.update(user_id, lambda p: add_fact(p, section, text, replaces))
