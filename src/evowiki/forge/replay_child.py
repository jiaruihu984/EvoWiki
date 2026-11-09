"""`python -m evowiki.forge.replay_child`: the replay half that runs the **patched** code (plan P3
T10, spec §5.2.3). Started by `forge.patch.replay.run_replay` in a Job Object, with the candidate's
worktree as cwd and `src` first on PYTHONPATH, with the secret-free test env: no API keys.

Protocol (JSON lines; stdout is the only channel to the parent):
  stdin, first line:  {"entry": str, "cases": [dict, ...]}
  stdout, per call:   {"op": "converse", "id": n, "request": <ChatRequest JSON>}
  stdin, the answer:  {"id": n, "turn": <ChatTurn JSON>, "call": <LLMCall JSON>} | {"id": n, "error": str}
  stdout, at the end: {"op": "done", "outputs": [dict | {"error": str}, ...]}, exit 0.

Anything the replayed code prints goes to stderr (sys.stdout is swapped before any entry runs), so
it cannot speak the protocol by accident. On purpose it still could (it runs in this process), which
is why the parent validates every line and trusts none of it."""

import importlib
import json
import sys
from collections.abc import Callable
from typing import Any, TextIO

from evowiki.forge import replay_entries
from evowiki.llm.chat import ChatRequest, ChatTurn
from evowiki.llm.models import LLMCall

ERROR_CHARS = 300


class ReplayCallError(RuntimeError):
    """The parent refused or failed a model call; the entry sees it as an exception."""


def _send(out: TextIO, msg: dict[str, Any]) -> None:
    out.write(json.dumps(msg, ensure_ascii=True, separators=(",", ":")) + "\n")
    out.flush()


class RpcConverse:
    """The child's `converse`: one request line out, one answer line in (blocking)."""

    def __init__(self, inp: TextIO, out: TextIO):
        self._inp, self._out, self._n = inp, out, 0

    def __call__(self, req: ChatRequest) -> tuple[ChatTurn, LLMCall]:
        self._n += 1
        _send(self._out, {"op": "converse", "id": self._n, "request": req.model_dump(mode="json")})
        line = self._inp.readline()
        if not line:
            raise ReplayCallError("parent closed the channel")
        msg = json.loads(line)
        if not isinstance(msg, dict) or msg.get("id") != self._n:
            raise ReplayCallError("answer does not match the request")
        if "error" in msg:
            raise ReplayCallError(str(msg["error"])[:ERROR_CHARS])
        return ChatTurn.model_validate(msg["turn"]), LLMCall.model_validate(msg["call"])


def _load(entry: str) -> Callable[..., Any]:
    """The only dynamic import in the forge: a fixed `module:function` from ENTRIES."""
    target = replay_entries.ENTRIES[entry]
    module, _, name = target.partition(":")
    return getattr(importlib.import_module(module), name)


def _error(e: BaseException) -> dict[str, str]:
    return {"error": f"{type(e).__name__}: {str(e)[:ERROR_CHARS]}"}


def main(inp: TextIO, out: TextIO) -> int:
    try:
        job = json.loads(inp.readline() or "null")
        entry, cases = job["entry"], job["cases"]
        if (not isinstance(entry, str) or entry not in replay_entries.ENTRIES
                or not isinstance(cases, list) or not all(isinstance(c, dict) for c in cases)):
            raise ValueError("bad job")
        fn = _load(entry)
    except Exception as e:  # noqa: BLE001 - a bad job is exit 2, never a protocol line
        print(f"replay_child: {_error(e)['error']}", file=sys.stderr)
        return 2
    converse = RpcConverse(inp, out)
    outputs: list[dict[str, Any]] = []
    for case in cases:
        try:
            res = fn(case, converse)
            if not isinstance(res, dict):
                raise TypeError(f"entry returned {type(res).__name__}, not dict")
            json.dumps(res)
        except Exception as e:  # noqa: BLE001 - one failed case is an output, not a crash
            res = _error(e)
        outputs.append(res)
    _send(out, {"op": "done", "outputs": outputs})
    return 0


if __name__ == "__main__":
    rpc = sys.stdout
    sys.stdout = sys.stderr                    # stray prints never reach the protocol channel
    raise SystemExit(main(sys.stdin, rpc))
