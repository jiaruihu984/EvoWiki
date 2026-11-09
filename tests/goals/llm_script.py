"""Scripted LLM for organizer-lane tests: a real Gateway (record mode, temp CallLog, one attempt
per input) over a ScriptedProvider that answers each request purpose from its own queue. Never
touches the network; an unscripted call fails the test.

A goals.intake
reply in the intake@v10 or v14–v17 shape is converted to the intake@v20 outline when pushed
(tests.goals.fakes_intake.to_outline). Entering a block (goals.enter) is answered from its own
queue, else from goals.intake's (one shared queue before S2), as `to_enter` shapes it."""

import json
from collections import defaultdict, deque
from pathlib import Path

from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.scripted import ScriptedProvider
from tests.goals.fakes_intake import (
    to_enter,
    to_outline,
)

_REF = ModelRef(provider="scripted", model="m")
class Script:
    def __init__(self) -> None:
        self._queues: dict[str, deque[str]] = defaultdict(deque)
        self._last: dict[str, str] = {}                     # purpose → last queued reply
        self.provider = ScriptedProvider("scripted", self._answer)

    def push(self, purpose: str, *outputs: dict | str) -> "Script":
        for o in outputs:
            if purpose == "goals.intake":
                o = to_outline(o)               # an older-shaped fixture: see fakes_intake
            self._queues[purpose].append(o if isinstance(o, str)
                                         else json.dumps(o, ensure_ascii=False))
        return self

    def _answer(self, model: str, request: LLMRequest) -> str:
        purpose = request.purpose
        if purpose == "goals.enter" and not self._queues[purpose]:
            purpose = "goals.intake"            # a fixture pushed in call order (module doc)
        queue = self._queues[purpose]
        if not queue:
            raise AssertionError(f"unscripted LLM call: {request.purpose}/{request.subject}")
        self._last[purpose] = queue.popleft()
        if request.purpose == "goals.enter":
            return to_enter(self._last[purpose], request.prompt)
        return self._last[purpose]

    def gateway(self, tmp_path: Path) -> Gateway:
        config = GatewayConfig(generator=_REF, verifier=_REF, allow_single_provider=True)
        return Gateway(config, CallLog(tmp_path / "llm_calls.jsonl"), mode="record",
                       providers={"scripted": self.provider}, record_max_attempts=1)

    def calls(self, purpose: str | None = None) -> int:
        return sum(purpose is None or r.purpose == purpose for r in self.provider.calls)

    def requests(self, purpose: str) -> list[LLMRequest]:
        return [r for r in self.provider.calls if r.purpose == purpose]
