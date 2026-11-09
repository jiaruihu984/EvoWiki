"""Patched-code replays (harness 3b, Task 8): the forge replay child's entries `deep.refine` and
`assistant.turn`, and the parent-side codec that turns a replay case into the JSON the child gets.

The child (`python -m evowiki.forge.replay_child`) runs the candidate's code with no keys and no
database. So everything a replay reads travels in the case:

- **deep.refine**: the user's wiki and aspect events up to the case's snapshot seqs, as the repo
  stores them, plus θ_infer of the snapshot's version (the K config is `k_config(infer)`; it is
  not in the logs), the teaching spec, the harness pack minus every `H10.*` key (P10: the child has
  one model route, the parent's deep tier), the component library the writer may place, and the
  parent's "now" (K's recall and the page context are read as of it). The child loads the events
  into a `MemoryRepo` (the same folding as `PgRepo.load_at`) and runs
  `DeepService.replay_section` on it: a dry run, no forged tools. A page whose concepts map to
  material sections is refused at encoding (`CaseNotEncodable`): the materials' reader and the
  citation check read the database, and a replay without them is not the run being compared.
- **assistant.turn** (P11: built, not selected in 3b): the traced first request of the turn and a
  cassette of its traced tool results. The child runs the agent loop with the assistant's limits
  on that request; each tool answers from the cassette by (name, canonical JSON args), else
  `MISSING_RECORD`. No repo, ledger or network.

Every model call goes through the child's `converse` (the parent RPC, `forge.replay_child`): the
deep tier's gateway in the child has one provider, `_RpcProvider`, that turns each provider call
into one `converse` request; the parent's converse owns routing and fallback, and counts the cost
(`cost` is None here). A refused or failed case is `{"case_id", "error"}`, never an exception.

This module is under `harness/` (denied to L2 patches, `forge.policy`): a patch can never change
how a case is rebuilt, only the code it runs."""

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from evowiki.agent.loop import ToolFinished, run_loop
from evowiki.agent.spec import AgentSpec, LoopLimits, RequestState
from evowiki.agent.tools import ToolContext, ToolResult, ToolSpec
from evowiki.agent.trace import request_from
from evowiki.belief.aspects import AspectEvent, AspectEventAdapter
from evowiki.deep.agent import PlanAnswers, SnapshotSeqs
from evowiki.deep.contracts import LibraryComponent
from evowiki.deep.library import ComponentLibrary, MemoryLibrary
from evowiki.deep.llm import DeepLLM
from evowiki.deep.store import MemoryAssetStore, MemoryJobStore
from evowiki.forge.binding import ForgeConverse
from evowiki.forge.patch.replay import REPLAY_PURPOSE, SUBJECT_MAX
from evowiki.goals.fold import Page
from evowiki.harness.pack import HarnessPack
from evowiki.harness.replay import Case, text_of
from evowiki.llm.chat import ChatMessage, ChatRequest, ChatResponse, ChatTurn, StreamSink
from evowiki.llm.gateway import Gateway
from evowiki.llm.models import GatewayConfig, LLMCall, LLMRequest, ModelRef, ProviderResponse
from evowiki.llm.prices import PriceTable
from evowiki.llm.usage import Account, NullMeter
from evowiki.sources.excerpts import SourceExcerpts
from evowiki.teach.contracts import InferParams, TeachingSpec
from evowiki.wikiapp.events import WikiEvent
from evowiki.wikiapp.refine_apply import KEEP_DOMAIN
from evowiki.wikiapp.refine_service import DeepService
from evowiki.wikiapp.refine_spec import OnlySpec
from evowiki.wikiapp.repo import MemoryRepo, Repo

Converse = Callable[[ChatRequest], tuple[ChatTurn, LLMCall]]

ERROR_CHARS = 300
H10_PREFIX = "H10."
RPC_PROVIDER = "parent"         # the child's only provider name: the parent's converse
RPC_REF = ModelRef(provider=RPC_PROVIDER, model="rpc")
RPC_CONFIG = GatewayConfig(generator=RPC_REF, verifier=RPC_REF, allow_single_provider=True)
# every model tier a pack's hook step may pick (H9 / H11 `tier`) goes to the same parent route:
# the child has one converse (P10)
RPC_TIERS = {"fast": RPC_CONFIG, "good": RPC_CONFIG}
MISSING_RECORD = "重放时没有这次工具调用的记录"
SCHEMA_PARAM = "json_schema"    # a JSON call's schema, carried in ChatRequest.params over the RPC
# the JSON calls a replay entry makes (`Gateway.call` in the agent code), by purpose: the parent
# remakes them with the same schema model, so the provider gets its JSON mode and schema text
JSON_CALLS: Mapping[str, type[BaseModel]] = MappingProxyType({"deep.plan": PlanAnswers})

# the request purposes each entry's code really sends (forge.replay_entries.PURPOSES): the deep
# writer's plan and write steps (deep/agent.py) and its H9 / H11 hook steps (harness/hooks.py,
# `hook.<at>`); the assistant entry replays the turn's own calls only (no hooks, compaction or
# carry-over: the traced first request already holds their results)
DEEP_PURPOSES = frozenset({"deep.plan", "deep.write", "hook.deep.before_write",
                           "hook.deep.after_write"})
ASSISTANT_PURPOSES = frozenset({"assistant.turn"})


class CaseNotEncodable(ValueError):
    """A case the child cannot replay faithfully (parent side, before any child starts)."""


def strip_h10(pack: HarnessPack) -> HarnessPack:
    """`pack` without its `H10.*` keys (P10): every step runs on the deep tier."""
    return HarnessPack(pack.version, {k: v for k, v in pack.values.items()
                                      if not k.startswith(H10_PREFIX)})


def _error(case: Mapping[str, Any], e: BaseException) -> dict[str, Any]:
    return {"case_id": case.get("case_id"), "error": f"{type(e).__name__}: {e}"[:ERROR_CHARS]}


# ---- parent side --------------------------------------------------------------------------


def _logs(conn: Any, repo: Repo, user_id: str,
          seqs: SnapshotSeqs) -> tuple[list[WikiEvent], list[AspectEvent]]:
    """The user's wiki / aspect events up to the snapshot seqs, as the repo stores them."""
    if isinstance(repo, MemoryRepo):
        return (repo.wiki_log(user_id)[:seqs.wiki_seq],
                repo.aspect_log(user_id)[:seqs.aspect_seq])
    from evowiki.store.aspects import aspect_events
    from evowiki.store.wiki import wiki_events
    wiki = [WikiEvent.model_validate({"event_id": s.event_id, "ts": s.occurred_at,
                                      "body": s.payload})
            for s in wiki_events(conn, user_id, upto_seq=seqs.wiki_seq)]
    return wiki, [s.event for s in aspect_events(conn, user_id, upto_seq=seqs.aspect_seq)]


def encode_deep_case(conn: Any, repo: Repo, case: Case, pack: HarnessPack, spec: TeachingSpec, *,
                     excerpts: SourceExcerpts | None,
                     library: ComponentLibrary | None = None,
                     now: datetime | None = None) -> dict[str, Any]:
    """The JSON the child's `deep_refine` replays `case` from (module doc). `conn`: a connection
    on the repo's database (unused for a MemoryRepo). `excerpts` (required): the deep service's
    own `sources`, so a page with materials is always refused; pass None only when the deployment
    has no material reader (`DeepService.sources is None`: then no page is written with materials
    either). `library`: the service's component library (None = none configured); `now`: the
    replay's "now" (default: the current time). CaseNotEncodable for a page with materials;
    LookupError when the snapshot or page is gone."""
    s = case.snapshot
    snap = repo.load_at(case.user_id, s.wiki_seq, s.aspect_seq, s.infer_version)
    rec = snap.state.pages.get(case.page_id)
    if rec is None:
        raise LookupError(f"快照里没有这个页面：{case.page_id}")
    if excerpts is not None and excerpts.for_nodes(case.user_id, list(rec.concepts)):
        raise CaseNotEncodable("这一页对应了资料，回放进程读不到资料，不能回放")
    wiki, aspect = _logs(conn, repo, case.user_id, s)
    out: dict[str, Any] = {
        "kind": "deep.refine",
        "case_id": case.case_id, "run_id": case.run_id, "user_id": case.user_id,
        "page_id": case.page_id, "section_id": case.section_id,
        "signal_ids": list(case.signal_ids), "anchor": case.anchor,
        "snapshot": asdict(s), "mode": case.mode, "instruction": case.instruction,
        "page": case.page, "page_seq": case.page_seq,
        "now": (now or datetime.now(UTC)).isoformat(),
        "pack": {"version": pack.version, "values": dict(strip_h10(pack).values)},
        "spec": spec.model_dump(mode="json"),
        "infer": snap.k.config.infer.model_dump(mode="json"),
        "wiki_events": [e.model_dump(mode="json") for e in wiki],
        "aspect_events": [e.model_dump(mode="json") for e in aspect],
        "library": None if library is None else [c.model_dump(mode="json")
                                                 for c in library.list()],
    }
    if case.domain is not KEEP_DOMAIN:
        out["domain"] = case.domain
    json.dumps(out)                          # JSON only: a value that is not fails here, not in the child
    return out


def encode_assistant_case(conn: Any, run_id: str) -> dict[str, Any]:
    """The traced first request of assistant turn `run_id` and its tool-result cassette (every
    traced tool step: name, args, result as the model got it). LookupError without a model step;
    `agent.trace.NotReplayable` when that request sent attachments."""
    from evowiki.store.trace import steps
    traced = steps(conn, run_id)
    first = next((s for s in traced if s["kind"] == "model"), None)
    if first is None:
        raise LookupError(f"run {run_id!r} has no model step")
    return {"kind": "assistant.turn", "case_id": run_id,
            "request": request_from(first).model_dump(mode="json"),
            "cassette": [{"name": s["name"], "args": s["args"], "result": s["result"]}
                         for s in traced if s["kind"] == "tool"]}


class _ReplayGateway:
    """The parent's side of a child's model calls (`parent_converse`): what `serve` relabelled is
    restored, then the call is made as the agent code makes it in process."""

    def __init__(self, gw: Gateway) -> None:
        self._gw = gw

    def converse(self, role: Any, request: ChatRequest, *, account: Account | None = None
                 ) -> tuple[ChatTurn, LLMCall]:
        req = restore(request)
        if SCHEMA_PARAM not in req.params:
            return self._gw.converse(role, req, account=account)
        params = dict(req.params)
        sent = params.pop(SCHEMA_PARAM)
        schema = JSON_CALLS.get(req.purpose)
        if schema is None or sent != schema.model_json_schema():
            raise ValueError(f"回放进程的 JSON 调用和这里的不一致：{req.purpose}")
        if (len(req.messages) != 1 or req.messages[0].role != "user" or req.tools
                or req.messages[0].parts):
            raise ValueError("回放进程的 JSON 调用不是一条用户消息")
        _, call = self._gw.call(role, LLMRequest(
            purpose=req.purpose, prompt_version=req.prompt_version, subject=req.subject,
            system=req.system, prompt=req.messages[0].text, params=params), schema,
            account=account)
        return ChatTurn(text=call.raw_output), call


def restore(request: ChatRequest) -> ChatRequest:
    """The child's own request from what `forge.patch.replay.serve` relabelled (purpose
    `forge.replay`, subject `<cid>:<purpose>:<subject>`), so per-purpose provider settings and
    cache markers see the real purpose. ValueError (the call is refused) when it is not one."""
    parts = request.subject.split(":", 2)
    if (request.purpose != REPLAY_PURPOSE or len(parts) != 3 or not all(parts[:2])
            or len(request.subject) >= SUBJECT_MAX):       # at the cap: maybe cut, not the real one
        raise ValueError(f"不是回放进程转来的请求：{request.purpose} / {request.subject[:80]}")
    return request.model_copy(update={"purpose": parts[1], "subject": parts[2]})


def parent_converse(gw: Gateway, account: Account, *, salt: str | None = None) -> ForgeConverse:
    """The converse to hand `forge.patch.replay.run_replay` / `serve` for a replay child: an
    accounted `ForgeConverse` that restores each request's purpose and subject, makes a JSON call
    (schema in `params`, `_RpcProvider.complete`) through `Gateway.call` with the same schema
    model (`JSON_CALLS`; the turn's text is the provider's raw output), and everything else through
    `Gateway.converse`. Calls are keyed by `salt` (default: a fresh one per converse), like an
    in-process replay's, so a child never gets a recorded call back instead of its own sample."""
    return ForgeConverse(gateway=_ReplayGateway(gw.salted(salt or f"forge-replay:{uuid4().hex}")),
                         account=account)


# ---- child side: deep.refine --------------------------------------------------------------


class _Calls:
    """The child's call log: in memory, for this process only (a replay is salted anyway)."""

    def __init__(self) -> None:
        self._by: dict[str, LLMCall] = {}
        self._rows: list[LLMCall] = []

    def get(self, input_hash: str) -> LLMCall | None:
        return self._by.get(input_hash)

    def taken(self, call_id: str) -> bool:
        return any(c.id == call_id for c in self._rows)

    def refuse(self, call_id: str) -> None:
        self._by = {h: c for h, c in self._by.items() if c.id != call_id}

    def append(self, call: LLMCall) -> None:
        if call.replayable:
            self._by.setdefault(call.input_hash, call)
        self._rows.append(call)

    def calls(self) -> list[LLMCall]:
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)


class _RpcProvider:
    """The child's only provider: each `chat` / `complete` is one `converse` request to the parent
    (a JSON call becomes a one-message chat request; its reply text is the raw output the gateway
    parses). Unpriced (cost None): the parent counts what was paid. `served[purpose]`: the provider
    of the parent's last call for that purpose (the writer family after the parent's fallback)."""

    name = RPC_PROVIDER
    streams = False

    def __init__(self, converse: Converse) -> None:
        self._converse = converse
        self.served: dict[str, str] = {}

    def _ask(self, req: ChatRequest) -> tuple[ChatTurn, LLMCall]:
        turn, call = self._converse(req)
        self.served[req.purpose] = call.provider
        return turn, call

    def chat(self, model: str, request: ChatRequest,
             stream: StreamSink | None = None) -> ChatResponse:
        turn, call = self._ask(request)
        return ChatResponse(turn=turn, model_version=call.model_version, cost=None,
                            input_tokens=turn.input_tokens, output_tokens=turn.output_tokens,
                            cache_read_tokens=turn.cache_read_tokens,
                            cache_write_tokens=turn.cache_write_tokens)

    def complete(self, model: str, request: LLMRequest,
                 json_schema: dict[str, Any]) -> ProviderResponse:
        turn, call = self._ask(ChatRequest(
            purpose=request.purpose, prompt_version=request.prompt_version,
            subject=request.subject, system=request.system,
            messages=(ChatMessage(role="user", text=request.prompt),),
            # the schema crosses the RPC in params; `parent_converse` pops it and makes the call
            # through the provider's JSON path (`Gateway.call`), as the in-process plan does
            params={**request.params, SCHEMA_PARAM: json_schema}))
        return ProviderResponse(raw_output=turn.text, model_version=call.model_version, cost=None,
                                input_tokens=turn.input_tokens, output_tokens=turn.output_tokens)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def _render_checker() -> Any:
    """What the deep service checks renders with (service.deps.build_deep): headless Chromium."""
    from evowiki.deep.render import PlaywrightChecker
    return PlaywrightChecker()


def deep_service(case: Mapping[str, Any], rpc: _RpcProvider,
                 checker: Any = None) -> tuple[DeepService, SnapshotSeqs]:
    """The child's deep service for one encoded case: a MemoryRepo of the shipped events, the deep
    tier on `rpc`, no trace sink, no forged tools, no materials; and the snapshot to replay (all
    the shipped events). `checker`: the render checker (default: the service's own)."""
    uid = case["user_id"]
    wiki = [WikiEvent.model_validate(e) for e in case["wiki_events"]]
    aspect = [AspectEventAdapter.validate_python(e) for e in case["aspect_events"]]
    infer_version = int(case["snapshot"]["infer_version"])
    repo = MemoryRepo.of_logs(uid, wiki, aspect, infer=InferParams.model_validate(case["infer"]),
                              infer_version=infer_version)
    now = datetime.fromisoformat(case["now"])

    def clock() -> datetime:
        return now

    spec = TeachingSpec.model_validate(case["spec"])
    library = (None if case.get("library") is None else
               MemoryLibrary([LibraryComponent.model_validate(c) for c in case["library"]]))
    llm = DeepLLM(config=RPC_CONFIG, providers={RPC_PROVIDER: rpc}, log=_Calls(),
                  prices=PriceTable({}), meter=NullMeter(), clock=clock, tiers=RPC_TIERS)
    svc = DeepService(repo=repo, jobs=MemoryJobStore(), assets=MemoryAssetStore(),
                      specs=OnlySpec(spec), checker=checker or _render_checker(), llm=llm,
                      clock=clock, new_id=_new_id, library=library)
    return svc, SnapshotSeqs(len(wiki), len(aspect), infer_version)


def deep_refine(case: dict[str, Any], converse: Converse, *,
                checker: Any = None) -> dict[str, Any]:
    """Entry `deep.refine`: the case's section rewritten by this process's code (a dry run of
    `DeepService.replay_section`) under the shipped pack and spec. `{"case_id", "text",
    "writer_provider", "cost": None, "run_id"}`, or `{"case_id", "error"}`."""
    try:
        rpc = _RpcProvider(converse)
        svc, snapshot = deep_service(case, rpc, checker)
        pack = HarnessPack(int(case["pack"]["version"]), case["pack"]["values"])
        out = svc.replay_section(
            case["user_id"], snapshot=snapshot, page_id=case["page_id"],
            section_id=case["section_id"], spec=TeachingSpec.model_validate(case["spec"]),
            harness=lambda _uid: pack, mode=case["mode"], instruction=case["instruction"],
            page=None if case.get("page") is None else Page.model_validate(case["page"]),
            domain=case.get("domain", KEEP_DOMAIN))
    except Exception as e:  # noqa: BLE001 - a failed case is an output (fail closed, visible)
        return _error(case, e)
    return {"case_id": case.get("case_id"), "text": text_of(out.body, out.cue),
            "writer_provider": rpc.served.get("deep.write", out.writer_provider), "cost": None,
            "run_id": out.run_id}


# ---- child side: assistant.turn -----------------------------------------------------------


def _canon(args: Any) -> str:
    return json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class CassetteTool:
    """One traced tool, answering from the cassette: the recorded results of a (name, args) in
    order, the last one again once they are used up; `MISSING_RECORD` for args never recorded."""

    def __init__(self, spec: ToolSpec, recorded: Mapping[str, list[ToolResult]]) -> None:
        self.spec = spec
        self._recorded = {k: list(v) for k, v in recorded.items()}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        got = self._recorded.get(_canon(args))
        if not got:
            return ToolResult(content=MISSING_RECORD, error="no recorded result")
        return got.pop(0) if len(got) > 1 else got[0]



def cassette_tools(request: ChatRequest,
                   cassette: Sequence[Mapping[str, Any]]) -> list[CassetteTool]:
    """A `CassetteTool` for every tool the request offered and every tool name the trace has."""
    by: dict[str, dict[str, list[ToolResult]]] = {}
    for rec in cassette:
        by.setdefault(rec["name"], {}).setdefault(_canon(rec["args"]), []).append(
            ToolResult.model_validate(rec["result"]))
    defs = {t.name: t for t in request.tools}
    out = []
    for name in dict.fromkeys([*defs, *by]):
        d = defs.get(name)
        # effect / schema are placeholders: nothing in the loop branches on them, and the result
        # always comes from the cassette (a tool only the trace names gets a generic schema)
        spec = ToolSpec(name=name, description=d.description if d else name,
                        parameters=d.parameters if d else {"type": "object"}, effect="read")
        out.append(CassetteTool(spec, by.get(name, {})))
    return out


def assistant_turn(case: dict[str, Any], converse: Converse) -> dict[str, Any]:
    """Entry `assistant.turn`: the traced turn's first request answered again by this process's
    agent loop under the assistant's limits, tools from the cassette. `{"case_id", "text"}` (the
    texts of every model call, joined as `run_turn` joins them), or `{"case_id", "error"}`."""
    from evowiki.assistant.loop import MAX_TOOL_CALLS, PURPOSE, REFUSED, SEGMENT_SEP
    try:
        req = ChatRequest.model_validate(case["request"])
        if req.purpose != PURPOSE:
            raise ValueError(f"not an assistant turn request: {req.purpose}")
        tools = cassette_tools(req, case.get("cassette") or ())
        parts: list[str] = []

        def request(state: RequestState) -> ChatRequest:
            return req.model_copy(update={"messages": state.messages,
                                          "tool_choice": state.tool_choice})

        def ask(r: ChatRequest) -> tuple[ChatTurn, LLMCall]:
            turn, call = converse(r)
            if turn.text.strip():
                parts.append(turn.text)
            return turn, call

        spec = AgentSpec(name=PURPOSE, tools=tools, request=request, limits=LoopLimits(
            max_tool_calls=MAX_TOOL_CALLS, on_cap="force_answer", text_is_answer=True,
            tool_error_max=12000, refused_text=REFUSED))
        ctx = ToolContext(user_id="replay", session_id=req.subject)
        gen = run_loop(spec, ask, ctx, req.messages)
        while True:
            try:
                ev = next(gen)
                if isinstance(ev, ToolFinished) and ev.result.error == "no recorded result":
                    raise LookupError(MISSING_RECORD)
            except StopIteration as stop:
                result = stop.value
                break
    except Exception as e:  # noqa: BLE001 - a failed case is an output (fail closed, visible)
        return _error(case, e)
    return {"case_id": case.get("case_id"), "text": SEGMENT_SEP.join(parts) or result.text}
