"""The replay child's dispatch table (plan P3 T10). `forge/**` is a denied path for patches, so the
copy the child imports from the candidate's worktree is byte-identical to the parent's (the parent
also checks this before it starts the child). Every target lives in a denied package (`forge`,
`harness`), so the code that rebuilds a run from a case is never the patched code; only what it
calls is.

An entry is `(case: dict, converse) -> dict`: it rebuilds one agent run from a recorded case and
returns JSON outputs, which the parent hands to the judge as data. Its model calls go through
`converse`, which in the child is the parent RPC. `PURPOSES[entry]` lists the request purposes the
entry may ask for; the parent kills a child that asks for any other one.

Harness 3b's entries `"deep.refine"` and `"assistant.turn"` live in `harness/replay_cases.py` (a
dry `DeepService.replay_section` on the shipped snapshot; the assistant loop on a traced request
with a tool-result cassette). The purpose sets are spelled out here, not imported, so the table
stays importable without the entries' dependencies; `tests/harness/test_replay_cases.py` checks
they equal `replay_cases.DEEP_PURPOSES` / `ASSISTANT_PURPOSES`."""

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any, Protocol

from evowiki.llm.chat import ChatMessage, ChatRequest, ChatTurn
from evowiki.llm.models import LLMCall

Converse = Callable[[ChatRequest], tuple[ChatTurn, LLMCall]]
PROBE_PURPOSE = "forge.replay.probe"


class ReplayEntry(Protocol):
    def __call__(self, case: dict[str, Any], converse: Converse) -> dict[str, Any]: ...


ENTRIES: Mapping[str, str] = MappingProxyType({
    "probe": "evowiki.forge.replay_entries:probe",
    "deep.refine": "evowiki.harness.replay_cases:deep_refine",
    "assistant.turn": "evowiki.harness.replay_cases:assistant_turn",
})
PURPOSES: Mapping[str, frozenset[str]] = MappingProxyType({
    "probe": frozenset({PROBE_PURPOSE}),
    # the deep writer's plan / write steps and its H9 / H11 hook steps (`hook.<at>`)
    "deep.refine": frozenset({"deep.plan", "deep.write", "hook.deep.before_write",
                              "hook.deep.after_write"}),
    # the turn's own calls only: the traced request already holds what hooks / compaction added
    "assistant.turn": frozenset({"assistant.turn"}),
})


def probe(case: dict[str, Any], converse: Converse) -> dict[str, Any]:
    """One model call; echoes the turn text (the mechanism's own smoke entry)."""
    q = case.get("q")
    if not isinstance(q, str) or not q:
        raise ValueError("probe case needs a non-empty string 'q'")
    turn, _ = converse(ChatRequest(purpose=PROBE_PURPOSE, prompt_version="forge-replay-probe@v1",
                                   subject="probe", system="Answer briefly.",
                                   messages=(ChatMessage(role="user", text=q),)))
    return {"text": turn.text}
