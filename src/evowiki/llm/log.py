from collections.abc import Sequence
from pathlib import Path
from threading import Lock
from typing import Protocol, runtime_checkable

from evowiki.llm.models import LLMAttempt, LLMCall


@runtime_checkable
class CallStore(Protocol):
    """Execution log of accepted LLM calls (jsonl or Postgres).

    Every accepted provider execution whose output is returned to or referenced by downstream
    runtime state has its own llm_call.id. Failed/unaccepted executions remain represented only
    by llm_attempt. input_hash stays the stable, nonce-free identity of the provider request;
    replayable rows remain the unique replay/cache corpus.

    `get` returns the unique *replayable* row for a hash (the replay corpus); non-replayable rows
    are audit-only and never returned by `get`. `calls`/`__len__` cover all rows. `taken(id)`:
    some row (of any kind) has that id — a cache row whose texts expired (store.retention) keeps
    the canonical id of its hash but is no longer replayable, so the next execution needs another.
    `refuse(id)`: code refused that answer after it parsed: the row stays, `get` no longer returns it."""

    def get(self, input_hash: str) -> LLMCall | None: ...
    def taken(self, call_id: str) -> bool: ...
    def refuse(self, call_id: str) -> None: ...
    def append(self, call: LLMCall) -> None: ...
    def calls(self) -> list[LLMCall]: ...
    def __len__(self) -> int: ...


@runtime_checkable
class AttemptStore(Protocol):
    """Audit of every provider attempt; never consulted for replay."""

    def append(self, attempt: LLMAttempt) -> None: ...
    def attempts(self) -> list[LLMAttempt]: ...
    def __len__(self) -> int: ...


class RequestLog(Protocol):
    """The full input (system, prompt) of a single-shot JSON call, keyed by its llm_call id: the
    call row keeps only the hash of its input (store/trace.py PgRequestLog)."""

    def record(self, call_id: str, system: str, prompt: str, schema_name: str, *,
               source_user_id: str | None = None, source_nodes: Sequence[str] = ()) -> None: ...


_OPTIONAL = ("user_id", "run_id", "served_from")     # left out of a line while None


def _defaulted(call: LLMCall) -> set[str]:
    """The fields a line leaves out: `replayable` when True and the observability fields while
    None, so a line is byte-identical to one written before they existed."""
    out = {f for f in _OPTIONAL if getattr(call, f) is None}
    return out | {"replayable"} if call.replayable else out


class CallLog:
    """Append-only JSONL store of LLM calls. Replayable rows are keyed by input_hash (unique);
    non-replayable rows may share a hash; ids are unique. A line omits the fields at their
    defaults (`_defaulted`), so it is byte-identical to lines written before they existed.
    `append` is thread-safe (concurrent calls, e.g. intake's split level, share one log)."""

    def __init__(self, path: Path):
        self._lock = Lock()
        self._path = Path(path)
        self._by_hash: dict[str, LLMCall] = {}  # replayable rows only
        self._rows: list[LLMCall] = []  # all rows, file order
        self._ids: set[str] = set()
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                call = LLMCall.model_validate_json(line)
                self._check(call)
                self._remember(call)

    def _check(self, call: LLMCall) -> None:
        if call.replayable and call.input_hash in self._by_hash:
            raise ValueError(f"duplicate input_hash in call log: {call.input_hash}")
        if call.id in self._ids:
            raise ValueError(f"duplicate llm_call id in call log: {call.id}")

    def _remember(self, call: LLMCall) -> None:
        if call.replayable:
            self._by_hash[call.input_hash] = call
        self._ids.add(call.id)
        self._rows.append(call)

    def get(self, input_hash: str) -> LLMCall | None:
        return self._by_hash.get(input_hash)

    def taken(self, call_id: str) -> bool:
        return call_id in self._ids

    def refuse(self, call_id: str) -> None:
        """For this process only: the file is append-only and offline tools read it whole."""
        with self._lock:
            for h, row in list(self._by_hash.items()):
                if row.id == call_id:
                    del self._by_hash[h]

    def append(self, call: LLMCall) -> None:
        line = call.model_dump_json(exclude=_defaulted(call))
        with self._lock:
            self._check(call)
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
            self._remember(call)

    def calls(self) -> list[LLMCall]:
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


class AttemptLog:
    """Append-only JSONL audit of every provider attempt (including failures).

    Several attempts may share an input_hash; ids are unique. Never consulted for replay.
    `append` is thread-safe."""

    def __init__(self, path: Path):
        self._lock = Lock()
        self._path = Path(path)
        self._attempts: list[LLMAttempt] = []
        self._ids: set[str] = set()
        if self._path.exists():
            for line in self._path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._remember(LLMAttempt.model_validate_json(line))

    def _remember(self, attempt: LLMAttempt) -> None:
        if attempt.id in self._ids:
            raise ValueError(f"duplicate attempt id in attempt log: {attempt.id}")
        self._ids.add(attempt.id)
        self._attempts.append(attempt)

    def append(self, attempt: LLMAttempt) -> None:
        line = attempt.model_dump_json()
        with self._lock:
            if attempt.id in self._ids:
                raise ValueError(f"duplicate attempt id in attempt log: {attempt.id}")
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
            self._remember(attempt)

    def attempts(self) -> list[LLMAttempt]:
        return list(self._attempts)

    def __len__(self) -> int:
        return len(self._attempts)
