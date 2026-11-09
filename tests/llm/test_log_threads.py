"""The JSONL call / attempt logs and the Gateway under concurrent calls (intake@v19 runs the split
calls of one level concurrently over one Gateway): every row is written once, whole, and is read
back; the CountingGateway counts every call."""

from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel

from evowiki.llm.gateway import Gateway
from evowiki.llm.log import AttemptLog, CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.scripted import ScriptedProvider
from evowiki.wikiapp.llm import CountingGateway

N = 64


class _Out(BaseModel):
    n: int


def test_concurrent_gateway_calls_write_every_row_whole(tmp_path):
    provider = ScriptedProvider("gen", lambda model, r: f'{{"n": {r.subject}}}')
    calls, attempts = CallLog(tmp_path / "c.jsonl"), AttemptLog(tmp_path / "a.jsonl")
    gw = CountingGateway(Gateway(
        GatewayConfig(generator=ModelRef(provider="gen", model="g"), verifier=ModelRef(
            provider="gen", model="g"), allow_single_provider=True),
        calls, mode="record", providers={"gen": provider}, attempts=attempts))

    def one(i: int) -> int:
        req = LLMRequest(purpose="unit", prompt_version="v1", subject=str(i), system="",
                         prompt=f"p{i}")
        return gw.call("generator", req, _Out)[0].n

    with ThreadPoolExecutor(max_workers=16) as pool:
        got = list(pool.map(one, range(N)))
    assert got == list(range(N)) and gw.calls == N
    assert len(calls) == len(attempts) == N
    reread_calls, reread_attempts = CallLog(tmp_path / "c.jsonl"), AttemptLog(tmp_path / "a.jsonl")
    assert sorted(c.subject for c in reread_calls.calls()) == sorted(str(i) for i in range(N))
    assert len(reread_attempts) == N
