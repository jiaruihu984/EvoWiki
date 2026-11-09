"""The forge's model binding (live-run finding 1): every forge model call carries the account it
is billed to, so `llm_call.cost` and `propose`'s `cost_usd` are real. `propose` refuses any other
converse (`ForgeMisconfigured`). The group is the caller's: "page" for request_tool in a page write,
"assistant" in a chat turn, "rsi" for a harness improver round.
RSI's forge.tool / forge.patch use EVOWIKI_IMPROVER_MODEL, independent of product tiers."""

from dataclasses import dataclass
from typing import Any, Protocol

from evowiki.llm.chat import ChatRequest, ChatTurn
from evowiki.llm.models import LLMCall
from evowiki.llm.usage import Account

ROLE = "generator"


class _Gw(Protocol):
    def with_model(self, ref: Any) -> "_Gw": ...

    def converse(self, role: Any, request: ChatRequest, *, account: Account | None = None
                 ) -> tuple[ChatTurn, LLMCall]: ...


@dataclass(frozen=True)
class ForgeConverse:
    gateway: _Gw
    account: Account

    def __call__(self, req: ChatRequest) -> tuple[ChatTurn, LLMCall]:
        gateway = self.gateway
        if self.account.group == "rsi" and req.purpose in ("forge.tool", "forge.patch"):
            from evowiki.runtime.usage import runtime_improver_ref

            ref = runtime_improver_ref()
            if ref is not None:
                gateway = gateway.with_model(ref)
        return gateway.converse(ROLE, req, account=self.account)


def accounted(converse: object) -> bool:
    return isinstance(converse, ForgeConverse) and isinstance(converse.account, Account)
