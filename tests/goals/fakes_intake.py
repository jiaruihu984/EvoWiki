"""Test doubles for the intake lane: a KView built on the prototype's Beta pseudo-counts (artifact
v5 `stats` / `band`), and a JsonLLM wired through the real Gateway with scripted providers (never
the network)."""

import json
import math
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from evowiki.goals.contracts import Aspect, Band, BandParams, CellBelief, NoteEntry, cell_id
from evowiki.goals.observe import JsonLLM, Role
from evowiki.llm.gateway import Gateway
from evowiki.llm.log import CallLog
from evowiki.llm.models import GatewayConfig, LLMRequest, ModelRef
from evowiki.llm.scripted import ScriptedProvider

PRIOR_A, PRIOR_B = 0.6, 1.4
P = BandParams()


class FakeK:
    """concept id -> aspects; each cell holds direct (a, b), inferred (ia, ib) and a declaration."""

    def __init__(self, concepts: Mapping[str, Sequence[Aspect | str]]):
        self._aspects = {c: tuple(Aspect(a) for a in aspects) for c, aspects in concepts.items()}
        self._k: dict[str, dict[str, Any]] = {
            cell_id(c, a): {"a": 0.0, "b": 0.0, "ia": 0.0, "ib": 0.0, "decl": None}
            for c, aspects in self._aspects.items() for a in aspects}
        self._notes: dict[str, list[NoteEntry]] = {}

    def add(self, cell: str, polarity: str, strength: float = 1.0) -> None:
        k = self._k[cell]
        if polarity == "+":
            k["a"] += strength
        elif polarity == "~":
            k["a"] += 0.4 * strength
            k["b"] += 0.8 * strength
        else:
            k["b"] += strength

    def declare(self, cell: str, value: str | None) -> None:
        self._k[cell]["decl"] = value

    def note_on(self, cell: str, text: str) -> None:
        c = cell.rpartition(".")[0]
        notes = self._notes.setdefault(c, [])
        notes.append(NoteEntry(entry_id=f"n{len(notes)}", cell=cell, text=text,
                               evidence_ref=f"ev{len(notes)}"))

    def aspects(self, concept_id: str) -> tuple[Aspect, ...]:
        return self._aspects.get(concept_id, ())

    def cell(self, cell: str) -> CellBelief:
        k = self._k[cell]
        a, b = PRIOR_A + k["a"] + k["ia"], PRIOR_B + k["b"] + k["ib"]
        n = a + b
        m, u = a / n, math.sqrt(a * b / (n * n * (n + 1)))
        direct = k["a"] + k["b"]
        if k["decl"] == "know":
            band = Band.KNOWN
        elif k["decl"] == "dont":
            band = Band.GAP
        elif direct < P.min_direct_evidence:
            band = Band.LIKELY if k["ia"] >= P.inferred_likely_min else Band.NONE
        elif m >= P.known_min_m and u <= P.known_max_u:
            band = Band.KNOWN
        elif m >= P.likely_min_m:
            band = Band.LIKELY
        elif m >= P.shaky_min_m:
            band = Band.SHAKY
        else:
            band = Band.GAP
        return CellBelief(cell=cell, m=m, u=u, direct=direct, inferred_pos=k["ia"],
                          inferred_neg=k["ib"], declared=k["decl"], band=band)

    def cells(self, concept_ids: tuple[str, ...]) -> dict[str, CellBelief]:
        return {cell_id(c, a): self.cell(cell_id(c, a))
                for c in concept_ids for a in self.aspects(c)}

    def note(self, concept_id: str) -> tuple[NoteEntry, ...]:
        return tuple(self._notes.get(concept_id, ()))


Responder = Callable[[str, str], str]


def scripted_llm(tmp_path: Path, gen: Responder, ver: Responder | None = None
                 ) -> tuple[JsonLLM, ScriptedProvider, ScriptedProvider]:
    """A JsonLLM over the real Gateway (record mode, scripted providers). generator and verifier
    are different providers, so a verifier call never sees the generator's request."""
    gen_p = ScriptedProvider("gen", lambda model, r: gen(r.purpose, r.prompt))
    ver_p = ScriptedProvider("ver", lambda model, r: (ver or gen)(r.purpose, r.prompt))
    gw = Gateway(GatewayConfig(generator=ModelRef(provider="gen", model="g"),
                               verifier=ModelRef(provider="ver", model="v")),
                 CallLog(tmp_path / "calls.jsonl"), mode="record",
                 providers={"gen": gen_p, "ver": ver_p})

    def call[T: BaseModel](role: Role, purpose: str, prompt: str, schema: type[T], *,
                           params: dict | None = None) -> T:
        req = LLMRequest(purpose=purpose, prompt_version="test", subject=purpose, system="",
                         prompt=prompt, params=params or {})
        return gw.call(role, req, schema)[0]

    return call, gen_p, ver_p


# a fixture's raw id of a NEW node (n1, r1, a1: one letter but c, then digits); else an existing id
_NEW_ID = re.compile(r"[abd-z]\d+(?:[a-z]+\d+)*")


ONE_LEVEL_MARK = "这一次只把下面这一块往下拆一层"


def to_parts(out: str) -> str:
    """An outline-shaped reply (`blocks` with `page`) as the one-level split's reply (intake@v31:
    `parts` with `atomic`): page true → atomic true, page false → atomic false, none → not
    judged; a reply already with `parts`, or not JSON, as it is."""
    try:
        data = json.loads(out)
    except ValueError:
        return out
    if not isinstance(data, dict) or "parts" in data or "blocks" not in data:
        return out

    def part(b: dict) -> dict:
        p = {k: v for k, v in b.items() if k != "page"}
        if "page" in b and "atomic" not in b:
            p["atomic"] = b["page"] is True
        return p
    return json.dumps({"title": data.get("title", ""),
                       "parts": [part(b) for b in data["blocks"] if isinstance(b, dict)],
                       "recommended": data.get("recommended", []),
                       **({"known": data["known"]} if "known" in data else {})},
                      ensure_ascii=False)


def to_outline(out: dict | str) -> dict | str:
    """A mapper reply written in the intake@v10 (`concepts` + `regions`) or intake@v14–v17 shape
    (`nodes` + `relations`) as the intake@v20 outline reply, so the flow fixtures keep what they
    meant: the nodes without a part_of parent are the first-level blocks (in the fixture's node
    order); a child's summary is its part_of phrase (the part_of phrase of intake@v19 is the
    child's summary), else its definition; a node with parts is page=false and its parts are stored under `_parts` (keyed by the
    node's existing id and by its normalized name) for the entered block's split (tests.wikiapp
    .script answers goals.enter from them); a raw id `n<k>` / `r<k>` / `a<k>` is a new node, any other id an existing one
    (`existing_id`); a block whose subtree has a target is marked target; prerequisite relations
    become `recommended` (intake@v20 writes no prerequisite: ends by existing id or name, phrase =
    why; a test that needs a prerequisite to feed inference verifies it in the graph, see
    tests/wikiapp/verify.py); kind_of / related are dropped (intake writes none). `title` and `observations` are kept, `_names` maps the raw ids to
    names (`known_from_mapper`). A reply already in the outline shape, or not a mapper reply, is
    returned as it is; a JSON string the same way (as a string)."""
    if isinstance(out, str):
        try:
            data = json.loads(out)
        except ValueError:
            return out
        if not isinstance(data, dict) or (new := to_outline(data)) is data:
            return out
        return json.dumps(new, ensure_ascii=False)
    if "blocks" in out:
        return out
    out = to_nodes(out)
    if not isinstance(out, dict) or "nodes" not in out:
        return out
    nodes = {n.get("id", ""): n for n in out["nodes"]}
    order = list(nodes)
    parents: dict[str, list[str]] = {}
    kids: dict[str, list[str]] = {}
    phrase: dict[tuple[str, str], str] = {}
    for r in out.get("relations", []):
        if r.get("kind") != "part_of" or r.get("from") == r.get("to"):
            continue
        src, dst = r.get("from", ""), r.get("to", "")
        for end in (src, dst):
            if end not in nodes:
                nodes[end] = {"id": end}
                order.append(end)
        if dst not in parents.setdefault(src, []):
            parents[src].append(dst)
            kids.setdefault(dst, []).append(src)
            phrase[(src, dst)] = r.get("phrase", "")

    def new(i: str) -> bool:
        return bool(_NEW_ID.fullmatch(i))

    def has_target(i: str, seen: frozenset = frozenset()) -> bool:
        return bool(nodes[i].get("target")) or any(
            has_target(k, seen | {i}) for k in kids.get(i, ()) if k not in seen)

    def block(i: str, *, top: bool, why: str = "") -> dict:
        n = nodes[i]
        b: dict = {} if new(i) or not i else {"existing_id": i}
        if n.get("name"):
            b["name"] = n["name"]
        if why or n.get("definition"):
            b["summary"] = why or n["definition"]
        if n.get("aspects"):
            b["aspects"] = n["aspects"]
        for k in ("sections", "exercises"):          # material section labels (intake@v23)
            if n.get(k):
                b[k] = n[k]
        b["page"] = i not in kids
        if top and has_target(i):
            b["target"] = True
        return b

    parts: dict[str, list[dict]] = {}
    for p, ks in kids.items():
        children = [block(k, top=False, why=phrase[(k, p)]) for k in ks]
        if not new(p):
            parts[p] = children
        if nodes[p].get("name"):
            parts["name:" + _norm(nodes[p]["name"])] = children

    def end(i: str) -> str:
        return i if not new(i) or i not in nodes else nodes[i].get("name", i)

    recommended = [{"from": end(r.get("from", "")), "to": end(r.get("to", "")),
                    "why": r.get("phrase", "")}
                   for r in out.get("relations", []) if r.get("kind") == "prerequisite"]
    rest = {k: v for k, v in out.items() if k not in ("nodes", "relations")}
    return {**rest, "blocks": [block(i, top=True) for i in order if not parents.get(i)],
            "recommended": recommended, "_parts": parts,
            "_names": {i: n.get("name", "") for i, n in nodes.items()}}


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", s).casefold())


# a numbered block of a split call (intake-expand@v8): `1. 「name」` and, for an existing node,
# `（Wiki 里已有的节点 id）`
_NUMBERED = re.compile(r"^(\d+)\. 「(.*?)」(?:（Wiki 里已有的节点 (\S+?)）)?$", re.MULTILINE)


def to_nodes(out: dict | str) -> dict | str:
    """A mapper reply written in the intake@v10 shape (`concepts` + `regions`) as the intake@v14+
    shape (`nodes` + `relations`): every concept and region is a node; a concept's `prereqs` and a
    region's `prereqs` become prerequisite relations, a region's `members` part_of relations;
    `relation_to_existing` prereq → a prerequisite from the existing node, none → related, both
    with the why as phrase, same → the node takes the existing id. Flow fixtures from before T8
    keep what they meant; a reply already in the node shape (or not a mapper reply) is returned
    as it is. `title` and `observations` (replayed by `known_from_mapper`) are kept. A JSON string
    (a recorded raw output) is converted the same way and returned as a string; text that is not
    a JSON object is returned unchanged."""
    if isinstance(out, str):
        try:
            data = json.loads(out)
        except ValueError:
            return out
        if not isinstance(data, dict) or (new := to_nodes(data)) is data:
            return out
        return json.dumps(new, ensure_ascii=False)
    if "nodes" in out or not ({"concepts", "regions"} & set(out)):
        return out
    same = {c.get("id"): r["existing"] for c in out.get("concepts", [])
            for r in c.get("relation_to_existing", []) if r.get("relation") == "same"}

    def i(x: str) -> str:
        return same.get(x, x)

    nodes, rels = [], []
    for c in out.get("concepts", []):
        nodes.append({**{k: c[k] for k in ("name", "target", "aspects", "subitems") if k in c},
                      "id": i(c.get("id", ""))})
        rels += [{"kind": "prerequisite", "from": i(p), "to": i(c.get("id", "")), "phrase": ""}
                 for p in c.get("prereqs", [])]
        for r in c.get("relation_to_existing", []):
            kind = {"prereq": "prerequisite", "none": "related"}.get(r.get("relation"))
            if kind is not None:
                rels.append({"kind": kind, "from": r.get("existing"), "to": c.get("id"),
                             "phrase": r.get("why", "")})
    for r in out.get("regions", []):
        nodes.append({k: r[k] for k in ("id", "name", "target") if k in r})
        rels += [{"kind": "part_of", "from": i(m), "to": r.get("id"), "phrase": ""}
                 for m in r.get("members", [])]
        rels += [{"kind": "prerequisite", "from": p, "to": r.get("id"), "phrase": ""}
                 for p in r.get("prereqs", [])]
    rest = {k: v for k, v in out.items() if k not in ("concepts", "regions")}
    return {**rest, "nodes": nodes, "relations": rels}


ESTIMATE_MARK = "再写 known："         # goals.enter's estimate part (goals/intake.ESTIMATE_BLOCK)
LEVEL_OF = {"+": "intermediate", "~": "beginner", "-": "unlearned"}


def known_from_mapper(mapper_reply: str | None) -> list[dict]:
    """The `known` entries of the S2 estimate a flow fixture meant: the `observations` it wrote
    into its goals.intake (mapper) reply — "the description says this" — by part name (the
    fixture's raw ids become names via `_names`; an existing id stays), polarity as a level
    (+ intermediate, ~ beginner, - unlearned). None: nothing known."""
    if not mapper_reply:
        return []
    out = to_outline(json.loads(mapper_reply))
    if not isinstance(out, dict):
        return []
    names = out.get("_names", {})
    known = []
    for o in out.get("observations", []):
        concept, aspect = o.get("concept") or "", o.get("aspect") or ""
        if not concept and "." in (o.get("cell") or ""):
            concept, _, aspect = o["cell"].rpartition(".")
        known.append({"part": names.get(concept, concept), "aspect": aspect,
                      "level": LEVEL_OF.get(o.get("polarity", ""), ""),
                      "quote": o.get("quote", "")})
    return known


def to_enter(reply: str, prompt: str) -> str:
    """A fixture pushed for an entered block as the goals.enter reply: outline-shaped → `parts`
    (`to_parts`); when the prompt has the estimate part and the reply no `known`, the fixture's
    observations as `known` (`known_from_mapper`); without the estimate part, no `known`."""
    out = to_parts(reply)
    try:
        data = json.loads(out)
    except ValueError:
        return out
    if not isinstance(data, dict):
        return out
    if ESTIMATE_MARK not in prompt:
        data.pop("known", None)
    elif "known" not in data:
        data["known"] = known_from_mapper(reply)
    return json.dumps(data, ensure_ascii=False)
