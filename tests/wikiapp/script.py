"""Scripted LLM for wikiapp tests: a real record-mode Gateway (in-memory call log, one attempt per
input) with BOTH runtime providers (anthropic = generator, deepseek = verifier) as
ScriptedProviders answering each request purpose from its own queue. Never touches the network;
an unscripted call fails the test loudly. A goals.intake reply written in the intake@v10
(concepts + regions) or v14–v17 shape (nodes + relations) is converted to the intake@v20 outline
when pushed (tests.goals.fakes_intake.to_outline), so older flow fixtures keep their meaning; and
`pop_mapper` hands it back as pushed (tests.wikiapp.flat_goal.region_goal). Entering a block
(goals.enter, S2) is answered from the goals.enter queue, else from the goals.intake queue — S1
and the entered block's split shared one purpose before S2, so the flow fixtures push both there
in call order: an outline-shaped reply is answered as `parts`, with the fixture's observations as
the estimate's `known` when the prompt asks for it (`to_enter`)."""

import json
from collections import defaultdict, deque

from evowiki.llm.gateway import Gateway
from evowiki.llm.models import LLMRequest
from evowiki.llm.scripted import ScriptedProvider
from tests.goals.fakes_intake import (
    to_enter,
    to_outline,
)
from tests.runtime.helpers import T0
from tests.runtime.llm_helpers import GW_CONFIG, MemoryCallLog

PROVIDERS = ("anthropic", "deepseek")


class Script:
    """Queued outputs per purpose; unscripted call -> AssertionError. `gateway()` builds a
    record-mode Gateway with BOTH providers (anthropic = generator, deepseek = verifier) over a
    MemoryCallLog."""

    latest: "Script | None" = None      # the most recent Script (service tests' page writer)

    def __init__(self) -> None:
        Script.latest = self
        self._queues: dict[str, deque[str]] = defaultdict(deque)
        self._served: list[tuple[str, LLMRequest]] = []     # (provider name, request)
        # purpose → fn(prompt) -> dict; the rule guard's judge (rule provenance contract, run by
        # every wired RSI round) answers no_conflict unless a test scripts it
        self._always: dict[str, object] = {
            "teach.rule_conflict": lambda prompt: {"verdict": "no_conflict", "why": "不冲突"}}
        self.log = MemoryCallLog()
        self._last: dict[str, str] = {}                     # purpose → last queued reply
        self._raw: deque[object] = deque()                  # goals.intake replies as pushed

    def push(self, purpose: str, *outputs: dict | str) -> "Script":
        for o in outputs:
            if purpose == "goals.intake":
                self._raw.append(o)
                o = to_outline(o)               # an older-shaped fixture: see fakes_intake
            self._queues[purpose].append(o if isinstance(o, str)
                                         else json.dumps(o, ensure_ascii=False))
        return self

    def always(self, purpose: str, fn) -> "Script":
        """Answer `purpose` with `fn(prompt) -> dict` whenever its queue is empty (a fake model
        that writes whatever the prompt asks, e.g. the probe's picked item)."""
        self._always[purpose] = fn
        return self

    def _responder(self, provider: str):
        def answer(model: str, request: LLMRequest) -> str:
            out = self.next_output(request)
            if out is None:
                raise AssertionError(
                    f"unscripted LLM call: {request.purpose}/{request.subject} via {provider}")
            self._served.append((provider, request))
            return out
        return answer

    def next_output(self, request: LLMRequest) -> str | None:
        """The queued output for `request` (popped), the `always` answer for its purpose, or None
        (unscripted)."""
        purpose = request.purpose
        if purpose == "goals.enter" and not self._queues.get(purpose) \
                and purpose not in self._always:
            purpose = "goals.intake"            # a flow fixture pushed in call order (module doc)
        queue = self._queues.get(purpose)
        out: str | None = None
        if queue:
            out = self._last[purpose] = queue.popleft()
            if purpose == "goals.intake":
                self._raw.popleft()
        elif purpose in self._always:
            got = self._always[purpose](request.prompt)   # type: ignore[operator]
            out = got if isinstance(got, str) else json.dumps(got, ensure_ascii=False)
        if out is not None and request.purpose == "goals.enter":
            return to_enter(out, request.prompt)
        return out

    def gateway(self) -> Gateway:
        return Gateway(GW_CONFIG, self.log, mode="record", clock=lambda: T0,
                       providers={p: ScriptedProvider(p, self._responder(p)) for p in PROVIDERS},
                       record_max_attempts=1)

    def take(self, purpose: str, prompt: str = "take") -> str:
        """The next scripted output of `purpose` without a Gateway call (test doubles that stand in
        for a whole component, e.g. the page writer), else its `always` answer for `prompt`;
        counted as served."""
        queue = self._queues.get(purpose)
        if not queue and purpose not in self._always:
            raise AssertionError(f"unscripted {purpose} (taken directly)")
        self._served.append(("script", LLMRequest(purpose=purpose, prompt_version="take",
                                                  subject="take", system="", prompt=prompt)))
        if queue:
            return queue.popleft()
        out = self._always[purpose](prompt)   # type: ignore[operator]
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)

    def pop_mapper(self) -> object:
        """The next goals.intake fixture as it was pushed (a mapper reply: tests.wikiapp.flat_goal
        .region_goal builds the eager map from it), taken from the queue."""
        if not self._queues.get("goals.intake"):
            raise AssertionError("no goals.intake fixture pushed")
        self._last["goals.intake"] = self._queues["goals.intake"].popleft()
        return self._raw.popleft()

    def calls(self, purpose: str | None = None) -> int:
        return sum(purpose is None or r.purpose == purpose for _, r in self._served)

    def roles(self, purpose: str) -> list[str]:
        """Provider names that served each call of `purpose`, in order."""
        return [p for p, r in self._served if r.purpose == purpose]

    def requests(self, purpose: str) -> list[LLMRequest]:
        return [r for _, r in self._served if r.purpose == purpose]

    def pending(self) -> dict[str, int]:
        """Scripted outputs not consumed yet, per purpose. The scripted assistant's diagnosis items
        (goal_script.PROBE, no model purpose since stage 3 T11) are asked only when a test answers."""
        return {k: len(q) for k, q in self._queues.items() if q and k != "goals.probe"}
