from evowiki.llm.gateway import Gateway
from evowiki.llm.models import LLMAttempt, LLMCall
from evowiki.llm.scripted import ScriptedProvider
from evowiki.runtime.llm import runtime_gateway_config
from tests.runtime.helpers import T0

GW_CONFIG = runtime_gateway_config({})


class MemoryCallLog:
    """Replayable rows keyed by hash with setdefault semantics (a second replayable call for a
    hash is silently dropped); non-replayable executions are all kept. calls() is append order."""

    def __init__(self):
        self._by: dict[str, LLMCall] = {}
        self._rows: list[LLMCall] = []

    def get(self, h):
        return self._by.get(h)

    def taken(self, call_id):
        return any(c.id == call_id for c in self._rows)

    def refuse(self, call_id):
        self._by = {h: c for h, c in self._by.items() if c.id != call_id}

    def append(self, call):
        if call.replayable:
            if call.input_hash in self._by:
                return
            self._by[call.input_hash] = call
        self._rows.append(call)

    def calls(self):
        return list(self._rows)

    def __len__(self):
        return len(self._rows)


class MemoryAttemptLog:
    def __init__(self):
        self._a: list[LLMAttempt] = []

    def append(self, a):
        self._a.append(a)

    def attempts(self):
        return list(self._a)

    def __len__(self):
        return len(self._a)


def scripted_gateway(responder, *, log=None, attempts=None, provider="anthropic") -> Gateway:
    return Gateway(GW_CONFIG, log if log is not None else MemoryCallLog(), mode="record",
                   attempts=attempts, clock=lambda: T0,
                   providers={provider: ScriptedProvider(provider, responder)})


class OverBudgetGateway:
    """A gateway whose every call is refused by the usage meter (BudgetExceededError)."""

    config = GW_CONFIG
    metered = True

    def __init__(self):
        self.calls = 0

    def call(self, role, request, schema, *, account=None):
        from evowiki.llm.usage import Account, BudgetExceededError
        self.calls += 1
        raise BudgetExceededError(Account(user_id="u", group="other"), "day", 1.0)
