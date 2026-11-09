"""Grounding constraints for V (spec §6.2): organization may be created, facts may not."""

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from evowiki.belief.beta import Belief
from evowiki.content.graph import ContentGraph
from evowiki.content.models import RENDERABLE_STATUSES, Lang
from evowiki.content.segments import SegmentIndex
from evowiki.view.constraints import persistent_role_allowed
from evowiki.view.log import Checker
from evowiki.view.ops import (
    Attach,
    CreateHub,
    Detach,
    MergeHub,
    OpArgs,
    RetireHub,
    SetMode,
    SetPerspective,
    SetRole,
    SurfaceLink,
)
from evowiki.view.state import ViewState

GROUNDING_MAX_HOPS: Final = 3
CONNECTION_REQUESTS_FILE: Final = "connection_requests.jsonl"


def concept_domains(graph: ContentGraph) -> dict[str, str]:
    return {c: graph.concept(c).domain for c in graph.concept_ids}


@dataclass(frozen=True)
class HubSupport:
    paths: tuple[tuple[str, ...], ...]
    missing_hops: tuple[tuple[str, str], ...]  # hops with no approved connection segment

    @property
    def complete(self) -> bool:
        return not self.missing_hops


def hub_support(members: Iterable[str], graph: ContentGraph, segments: SegmentIndex,
                max_hops: int = GROUNDING_MAX_HOPS) -> HubSupport | None:
    """Support paths for a member set (prerequisite_of/related_to only, ≤ max_hops per pair)
    and the hops no approved connection segment covers. None if not path-supported."""
    paths = graph.support_paths(members, max_hops)
    if paths is None:
        return None
    hops = sorted({tuple(sorted(h)) for p in paths for h in pairwise(p)})
    missing = tuple((a, b) for a, b in hops if not segments.connections_covering(a, b))
    return HubSupport(paths, missing)


def _support_violations(members: Iterable[str], graph: ContentGraph,
                        segments: SegmentIndex) -> list[str]:
    members = sorted(set(members))
    support = hub_support(members, graph, segments)
    if support is None:
        return [(f"members {members} are not path-supported within {GROUNDING_MAX_HOPS} hops "
                 "over prerequisite_of/related_to")]
    return [f"no verified connection segment for hop {a}-{b}" for a, b in support.missing_hops]


def check_op(args: OpArgs, state: ViewState, graph: ContentGraph,
             segments: SegmentIndex) -> list[str]:
    """Every grounding violation of `args` against `state` (empty list = grounded)."""
    known = set(graph.concept_ids)
    out: list[str] = []

    def concept(cid: str) -> None:
        if cid not in known:
            out.append(f"unknown concept {cid}")

    def active(hub_id: str) -> bool:
        if hub_id not in state.active_hubs():
            out.append(f"hub {hub_id} is not active")
            return False
        return True

    match args:
        case SetMode():
            concept(args.concept_id)
        case SetRole():
            concept(args.concept_id)
            if args.anchor is not None:
                concept(args.anchor)
        case SetPerspective():
            concept(args.concept_id)
            ids = {p.segment_id for p in graph.perspectives_for(args.concept_id)} \
                if args.concept_id in known else set()
            try:
                renderable = segments.segment(args.segment_id).review_status in RENDERABLE_STATUSES
            except KeyError:
                renderable = False
            if args.segment_id not in ids or not renderable:
                out.append(f"{args.segment_id} is not a verified perspective of {args.concept_id}")
        case CreateHub():
            if args.hub_id in state.hubs:
                out.append(f"hub {args.hub_id} already exists")
            for m in args.members:
                concept(m)
            if not out:
                out += _support_violations(args.members, graph, segments)
                support = hub_support(args.members, graph, segments)
                if not out and support is not None and args.support_paths != support.paths:
                    out.append("support_paths do not match G's shortest paths")
        case Attach():
            concept(args.concept_id)
            if active(args.hub_id) and not out:
                out += _support_violations(state.members(args.hub_id) | {args.concept_id},
                                           graph, segments)
        case Detach():
            if args.hub_id not in state.concept(args.concept_id).attachments:
                out.append(f"{args.concept_id} is not attached to {args.hub_id}")
            elif len(state.members(args.hub_id) - {args.concept_id}) < 2:
                out.append(f"detaching {args.concept_id} leaves hub {args.hub_id} with fewer "
                           "than 2 members")
        case SurfaceLink():
            concept(args.concept_id)
            concept(args.other_concept_id)
            if not out and not any(
                c.segment_id == args.segment_id
                for c in segments.connections_covering(args.concept_id, args.other_concept_id)
            ):
                out.append(f"{args.segment_id} is not a verified connection covering "
                           f"{args.concept_id} and {args.other_concept_id}")
        case RetireHub():
            active(args.hub_id)
        case MergeHub():
            ok = active(args.hub_id) & active(args.absorbed_hub_id)
            if ok:
                out += _support_violations(
                    state.members(args.hub_id) | state.members(args.absorbed_hub_id),
                    graph, segments)
        case _:
            out.append(f"unknown op {args!r}")
    return out


def grounding_checker(graph: ContentGraph, segments: SegmentIndex) -> Checker:
    def check(args: OpArgs, state: ViewState) -> list[str]:
        return check_op(args, state, graph, segments)

    return check


def role_eligibility(beliefs: Mapping[str, Belief]) -> Callable[[OpArgs], list[str]]:
    """Persistent-role invariant (spec §1.4, layer A): support/reference/bridge need mastered."""
    def check(args: OpArgs) -> list[str]:
        if isinstance(args, SetRole) and not persistent_role_allowed(args.role,
                                                                     beliefs[args.concept_id]):
            return [f"role {args.role} for {args.concept_id} requires mastered"]
        return []
    return check


def hub_explanation_segments(hub_id: str, state: ViewState, segments: SegmentIndex,
                             lang: Lang = Lang.EN) -> tuple[str, ...]:
    """Segments a hub's explanation may be built from (spec §6.2.3): each member's refresher
    segment and one approved connection per support-path hop. Nothing else. A connection in
    `lang` is preferred, then English, then the first approved one (sorted by segment id)."""
    out: list[str] = []
    for m in sorted(state.members(hub_id)):
        seg = segments.prereq_segment(m, lang)
        if seg is not None:
            out.append(seg.id)
    for p in state.hubs[hub_id].support_paths:
        for a, b in pairwise(p):
            covering = [c.segment_id for c in segments.connections_covering(a, b)]
            if covering:
                out.append(_prefer_lang(covering, segments, lang))
    return tuple(dict.fromkeys(out))


def _prefer_lang(segment_ids: list[str], segments: SegmentIndex, lang: Lang) -> str:
    for lg in (lang, Lang.EN):
        for sid in segment_ids:
            if segments.segment(sid).lang is lg:
                return sid
    return segment_ids[0]


class ConnectionRequest(BaseModel):
    """Ask the content pipeline for a connection segment on `path` (spec §6.2.4)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: tuple[str, ...] = Field(min_length=2, max_length=4)
    requested_for: str


def append_connection_requests(path: Path, requests: Iterable[ConnectionRequest]) -> int:
    """Append requests whose path is not queued yet; returns how many were added."""
    path = Path(path)
    seen: set[tuple[str, ...]] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                seen.add(tuple(json.loads(line)["path"]))
    added = []
    for r in requests:
        if r.path not in seen:
            seen.add(r.path)
            added.append(r)
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as fh:
            for r in added:
                fh.write(r.model_dump_json() + "\n")
    return len(added)
