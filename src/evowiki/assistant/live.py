"""The live side channel of a durable assistant turn (spec 2026-10-15 §3).

A DBOS stream may only be written from the workflow body, but a model's tokens arrive inside its
step. So the tokens of the call in flight go to this in-process bus instead, keyed by workflow id;
the workflow body writes the committed text of the call to the DBOS stream after the step
returns, and the follower (`turn_runs`) merges both.

One snapshot per turn: `(base_index, base_at, text)`. `base_index` is the position in the
DBOS stream the call's committed text will take, `base_at` its character position in the turn's
answer, `text` is what was streamed so far (with the paragraph separator the committed text will
have). Streamed text is never taken back: the gateway does not retry an attempt that showed text. Memory only: one service process
(spec 2026-10-14 §1); after a crash the follower falls back to the durable stream."""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace

log = logging.getLogger("evowiki.assistant.live")

Poke = Callable[[], None]


@dataclass(frozen=True)
class Snapshot:
    base_index: int
    base_at: int
    text: str


class LiveSink:
    """`StreamSink` of one model call: every chunk updates the turn's snapshot."""

    def __init__(self, bus: "LiveBus", key: str, base_index: int, base_at: int, prefix: str):
        self._bus, self._key, self._prefix = bus, key, prefix
        bus._put(key, Snapshot(base_index, base_at, ""))

    def text(self, chunk: str) -> None:
        if chunk:
            self._bus._update(self._key, lambda s: replace(
                s, text=(s.text or self._prefix) + chunk))


class LiveBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snaps: dict[str, Snapshot] = {}
        self._subs: dict[str, list[Poke]] = {}
        self._status: dict[str, tuple[int, str]] = {}   # a long tool's step line, numbered
        self._stops: set[str] = set()    # turns he stopped: the call in flight is cut at once
        self._seq = 0

    def sink(self, key: str, base_index: int, base_at: int, prefix: str) -> LiveSink:
        return LiveSink(self, key, base_index, base_at, prefix)

    def source(self, key: str) -> Callable[[int, int, str], LiveSink]:
        """What a turn body takes: one sink per model call."""
        return lambda base_index, base_at, prefix: self.sink(key, base_index, base_at, prefix)

    def snapshot(self, key: str) -> Snapshot | None:
        with self._lock:
            return self._snaps.get(key)

    def subscribe(self, key: str, poke: Poke) -> Callable[[], None]:
        with self._lock:
            self._subs.setdefault(key, []).append(poke)

        def unsubscribe() -> None:
            with self._lock:
                subs = self._subs.get(key, [])
                if poke in subs:
                    subs.remove(poke)
                if not subs:
                    self._subs.pop(key, None)
        return unsubscribe

    def status(self, key: str, text: str) -> None:
        """What a running tool of the turn is doing (one plain line; "" clears it). Followers hand
        it out as a StatusLine; it is never written to the durable stream (a resumed follower
        gets only the current one)."""
        with self._lock:
            cur = self._status.get(key)
            if (cur is None and not text) or (cur is not None and cur[1] == text):
                return
            self._seq += 1
            self._status[key] = (self._seq, text)
            subs = list(self._subs.get(key, ()))
        self._poke(subs)

    def status_of(self, key: str) -> tuple[int, str]:
        with self._lock:
            return self._status.get(key, (0, ""))

    def stop(self, key: str) -> None:
        with self._lock:
            self._stops.add(key)

    def stopped(self, key: str) -> bool:
        with self._lock:
            return key in self._stops

    def end(self, key: str) -> None:
        """The turn is over: its snapshot goes (the durable stream holds everything)."""
        with self._lock:
            self._snaps.pop(key, None)
            self._status.pop(key, None)
            self._stops.discard(key)
            subs = list(self._subs.get(key, ()))
        self._poke(subs)

    def _put(self, key: str, snap: Snapshot) -> None:
        with self._lock:
            self._snaps[key] = snap
            subs = list(self._subs.get(key, ()))
        self._poke(subs)

    def _update(self, key: str, fn: Callable[[Snapshot], Snapshot]) -> None:
        with self._lock:
            cur = self._snaps.get(key)
            if cur is None:
                return
            self._snaps[key] = fn(cur)
            subs = list(self._subs.get(key, ()))
        self._poke(subs)

    @staticmethod
    def _poke(subs: list[Poke]) -> None:
        for poke in subs:
            try:
                poke()
            except Exception:  # a follower's queue never fails the turn
                log.warning("live follower poke failed", exc_info=True)


BUS = LiveBus()
