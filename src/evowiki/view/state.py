"""V_{u,t} (spec §4.4 view_state + hubs): a pure fold target. Membership == attachment."""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Literal

from evowiki.view.ops import (
    Attach,
    CreateHub,
    Detach,
    MergeHub,
    Mode,
    OpArgs,
    RetireHub,
    SetMode,
    SetPerspective,
    SetRole,
    SurfaceLink,
)
from evowiki.view.roles import Role


class InapplicableOp(ValueError):
    """The op does not apply to the current state (unknown hub, duplicate link, ...)."""


@dataclass(frozen=True)
class Hub:
    id: str
    label_en: str
    label_zh: str
    support_paths: tuple[tuple[str, ...], ...]
    status: Literal["active", "retired", "merged"] = "active"
    merged_into: str | None = None


def _ro(d: Mapping[str, float]) -> Mapping[str, float]:
    return MappingProxyType(dict(sorted(d.items())))


@dataclass(frozen=True)
class ConceptView:
    mode: Mode | None = None                # legacy axis (load-only SET_MODE); never a role
    perspective_segment_id: str | None = None
    attachments: Mapping[str, float] = field(default_factory=lambda: MappingProxyType({}))
    links: tuple[tuple[str, str], ...] = ()  # (other concept, connection segment id), sorted
    role: Role | None = None                # persistent role (SET_ROLE)
    role_anchor: str | None = None          # support/bridge anchor concept


_EMPTY = ConceptView()


def _concept_dict(v: ConceptView) -> dict[str, Any]:
    d: dict[str, Any] = {"mode": v.mode, "perspective": v.perspective_segment_id,
                         "attachments": dict(v.attachments), "links": [list(x) for x in v.links]}
    if v.role is not None:  # role-free states keep their exact pre-SET_ROLE dict
        d["role"] = v.role.value
        d["role_anchor"] = v.role_anchor
    return d


class ViewState:
    """Immutable personal view. `apply` returns a new state and never mutates this one."""

    def __init__(self, hubs: Mapping[str, Hub] | None = None,
                 concepts: Mapping[str, ConceptView] | None = None, applied: int = 0):
        self._hubs = dict(hubs or {})
        self._concepts = dict(concepts or {})
        self._applied = applied

    # --- read side -------------------------------------------------------------------------
    @property
    def applied(self) -> int:
        return self._applied

    @property
    def version(self) -> str:
        return f"view@{self._applied}"

    @property
    def hubs(self) -> Mapping[str, Hub]:
        return MappingProxyType(self._hubs)

    def concept(self, cid: str) -> ConceptView:
        return self._concepts.get(cid, _EMPTY)

    def concept_ids(self) -> list[str]:
        return sorted(self._concepts)

    def members(self, hub_id: str) -> frozenset[str]:
        return frozenset(c for c, v in self._concepts.items() if hub_id in v.attachments)

    def _active(self, hub_id: str) -> bool:
        hub = self._hubs.get(hub_id)
        return hub is not None and hub.status == "active"

    def active_hubs(self) -> dict[str, frozenset[str]]:
        return {h: self.members(h) for h in sorted(self._hubs) if self._active(h)}

    def primary_hub(self, cid: str, active_domain: str | None,
                    domain_of: Mapping[str, str]) -> str | None:
        """The active attachment maximizing (share of members in the active domain, salience);
        ties go to the smallest hub id (spec §5.1 step 2, §6.1)."""
        att = {h: s for h, s in self.concept(cid).attachments.items() if self._active(h)}
        if not att:
            return None

        def relevance(h: str) -> float:
            if active_domain is None:
                return 0.0
            members = self.members(h)
            return sum(domain_of[m] == active_domain for m in members) / len(members)

        return max(sorted(att), key=lambda h: (relevance(h), att[h]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self._applied,
            "hubs": {k: vars(v) | {"support_paths": [list(p) for p in v.support_paths]}
                     for k, v in sorted(self._hubs.items())},
            "concepts": {k: _concept_dict(v) for k, v in sorted(self._concepts.items())
                         if v != _EMPTY},
        }

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ViewState) and self.to_dict() == other.to_dict()

    __hash__ = None  # type: ignore[assignment]

    # --- write side (pure) -------------------------------------------------------------------
    def apply(self, args: OpArgs) -> "ViewState":
        hubs, concepts = dict(self._hubs), dict(self._concepts)

        def put(cid: str, **changes: Any) -> None:
            concepts[cid] = replace(concepts.get(cid, _EMPTY), **changes)

        def attach(cid: str, hub_id: str, salience: float) -> None:
            att = dict(concepts.get(cid, _EMPTY).attachments)
            att[hub_id] = salience
            put(cid, attachments=_ro(att))

        def need_active(hub_id: str) -> None:
            if not self._active(hub_id):
                raise InapplicableOp(f"hub {hub_id} is not active")

        match args:
            case SetMode():
                put(args.concept_id, mode=args.mode)
            case SetRole():
                put(args.concept_id, role=args.role, role_anchor=args.anchor)
            case SetPerspective():
                put(args.concept_id, perspective_segment_id=args.segment_id)
            case CreateHub():
                if args.hub_id in hubs:
                    raise InapplicableOp(f"hub {args.hub_id} already exists")
                hubs[args.hub_id] = Hub(args.hub_id, args.label_en, args.label_zh,
                                        args.support_paths)
                for m in args.members:
                    attach(m, args.hub_id, args.salience)
            case Attach():
                need_active(args.hub_id)
                attach(args.concept_id, args.hub_id, args.salience)
            case Detach():
                att = dict(self.concept(args.concept_id).attachments)
                if args.hub_id not in att:
                    raise InapplicableOp(f"{args.concept_id} is not attached to {args.hub_id}")
                del att[args.hub_id]
                put(args.concept_id, attachments=_ro(att))
            case SurfaceLink():
                link = (args.other_concept_id, args.segment_id)
                links = self.concept(args.concept_id).links
                if link in links:
                    raise InapplicableOp(f"link {link} already surfaced on {args.concept_id}")
                put(args.concept_id, links=tuple(sorted((*links, link))))
            case RetireHub():
                need_active(args.hub_id)
                hubs[args.hub_id] = replace(hubs[args.hub_id], status="retired")
            case MergeHub():
                need_active(args.hub_id)
                need_active(args.absorbed_hub_id)
                hubs[args.absorbed_hub_id] = replace(hubs[args.absorbed_hub_id], status="merged",
                                                     merged_into=args.hub_id)
                for cid, view in self._concepts.items():
                    if args.absorbed_hub_id in view.attachments:
                        att = dict(view.attachments)
                        moved = att.pop(args.absorbed_hub_id)
                        att[args.hub_id] = max(moved, att.get(args.hub_id, 0.0))
                        put(cid, attachments=_ro(att))
            case _:
                raise TypeError(f"unknown op {args!r}")
        return ViewState(hubs, concepts, self._applied + 1)
