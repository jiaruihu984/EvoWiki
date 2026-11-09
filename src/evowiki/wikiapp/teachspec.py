"""Teaching spec θ_rep,u (text part) as pure functions: the migration of the old 原则 (guidelines),
the effective spec of a wiki state, which items apply to a page, and edit operations.

Every edit returns a NEW `TeachingSpec` with `version + 1` (one stored snapshot per version, see
`wikiapp.events.TeachingSpecSaved`); caps (30 items, 3 exemplars, text/domain lengths, repeated ids)
raise `SpecInvalid` (a ValueError; the service maps it to 422). An unknown item / exemplar id raises
`LookupError` (→ 404). Nothing here does IO or calls an LLM."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from evowiki.rules.authority import Actor, check
from evowiki.rules.decisions import NO_DECISIONS, Decisions
from evowiki.teach.contracts import (
    MAX_ITEM_TEXT,
    Authority,
    Exemplar,
    Origin,
    RepParams,
    SpecItem,
    SpecScope,
    TeachingSpec,
)
from evowiki.teach.default_spec import DEFAULT_ITEMS
from evowiki.teach.render import guideline_lines, item_text
from evowiki.wikiapp.events import Guideline
from evowiki.wikiapp.state import WikiState

MAX_EXEMPLARS = 3
MAX_DOMAIN = 60

PageKind = Literal["quick", "concept"]
QUICK_SCOPES: frozenset[SpecScope] = frozenset({"any", "quick"})
CONCEPT_SCOPES: frozenset[SpecScope] = frozenset({"any", "concept", "exercise"})

TOO_MANY_EXEMPLARS = f"范例最多 {MAX_EXEMPLARS} 个"


class SpecInvalid(ValueError):
    """An edit would break a cap or the spec's shape (→ 422)."""

    def __init__(self, message: str = "", *, public_message: str | None = None,
                 code: str = "teaching_spec_invalid", params: dict[str, object] | None = None):
        super().__init__(message)
        self.public_message, self.error_code, self.params = public_message, code, params or {}


class _Keep:
    def __repr__(self) -> str:
        return "KEEP"


KEEP = _Keep()                # edit_item: leave the domain as it is (None clears it)


# ---- migration / effective spec ----

def migrate(guidelines: Iterable[Guideline]) -> TeachingSpec:
    """Version 1 for a user: the default items first, then the old guidelines in order
    (`user` → origin `user_ui`; `feedback` → origin `legacy_unknown` with origin_ref
    `guideline:{id}`; both authority `user`, ids kept, scope `any`, domain None). No item count cap:
    how many rules is the user's choice."""
    migrated = tuple(
        SpecItem(item_id=g.guideline_id, text=g.text, scope="any",
                 origin="user_ui" if g.source == "user" else "legacy_unknown", authority="user",
                 origin_ref=None if g.source == "user" else f"guideline:{g.guideline_id}")
        for g in guidelines)
    return _build(1, (*DEFAULT_ITEMS, *migrated), (), RepParams())


def migration_evidence(guidelines: Iterable[Guideline]) -> dict[str, str]:
    """Evidence of the feedback guidelines, for `TeachingSpecSaved.evidence` on the migrated v1."""
    return {g.guideline_id: g.evidence for g in guidelines
            if g.source == "feedback" and g.evidence}


def effective_spec(state: WikiState) -> TeachingSpec:
    """The stored spec, else the virtual v1 (defaults + migrated guidelines)."""
    return state.spec if state.spec is not None else migrate(state.guidelines)


def effective_version(state: WikiState) -> int:
    return state.spec.version if state.spec is not None else 1


# ---- which items apply to a page ----

def page_kind(state: WikiState, page_id: str) -> PageKind:
    """`quick` when the page is shelved as a reference card, else `concept`."""
    rec = state.pages.get(page_id)
    return "quick" if rec is not None and rec.shelved else "concept"


def norm_domain(domain: str | None) -> str | None:
    """Stripped; empty → None."""
    if domain is None:
        return None
    d = domain.strip()
    return d or None


def _same_domain(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def applicable(spec: TeachingSpec, kind: PageKind, domain: str | None) -> tuple[SpecItem, ...]:
    """Items in force for a page, in spec order. Scopes: quick → {any, quick}; concept →
    {any, concept, exercise}. An item without a domain always applies; a domain item when the
    page's domain equals it (casefold, stripped), or when the page has no domain (none stored, or
    the embedding pick found no label near enough): it then goes to the writer with its domain
    named (`teach.render.item_text`) and the writer, who sees the page, judges whether it fits.
    Only a page whose domain is another one leaves it out. A pending item (waiting for the learner's
    confirmation, `wikiapp.preferences`) never applies: it teaches nothing until he confirms it."""
    scopes = QUICK_SCOPES if kind == "quick" else CONCEPT_SCOPES
    d = norm_domain(domain)
    return tuple(i for i in spec.items if i.scope in scopes and i.authority != "pending"
                 and (i.domain is None or d is None or _same_domain(i.domain, d)))


def spec_domains(spec: TeachingSpec) -> tuple[str, ...]:
    """The distinct item domains (first spelling wins, spec order); empty → no domain call needed."""
    seen: dict[str, str] = {}
    for i in spec.items:
        if i.domain is not None:
            seen.setdefault(i.domain.strip().casefold(), i.domain.strip())
    return tuple(seen.values())


@dataclass(frozen=True)
class PageTeach:
    """The teaching spec in force for one page: the applicable items (spec order), the params and
    the effective spec version. `guidelines` is what `PageSpec.guidelines` carries (item texts,
    then the 4 param lines); `item_ids` is what a SectionLog records as `spec_items`."""

    items: tuple[SpecItem, ...]
    params: RepParams
    version: int

    @property
    def item_ids(self) -> tuple[str, ...]:
        return tuple(i.item_id for i in self.items)

    @property
    def guidelines(self) -> tuple[str, ...]:
        return guideline_lines(self.items, self.params)


def page_teach(state: WikiState, page_id: str,
               domain: str | None | _Keep = KEEP) -> PageTeach:
    """What applies to `page_id` (KeyError if no such page): its kind from the reference-card
    structure, its domain (`PageRecord.domain`, or `domain` when given, e.g. one just picked and
    not stored yet). SpecInvalid when the effective spec cannot be built (>30 old guidelines)."""
    rec = state.pages[page_id]
    spec = effective_spec(state)
    d = rec.domain if isinstance(domain, _Keep) else domain
    return PageTeach(items=applicable(spec, page_kind(state, page_id), d), params=spec.params,
                     version=effective_version(state))


def wiki_texts(state: WikiState, *, domains: bool = False) -> tuple[str, ...]:
    """Wiki-wide principle texts, spec order, no pending item: for the curator the effective
    spec's items without a domain (any scope); with `domains` (the assistant, which answers about
    any page) every item, a domain item with its domain named (`teach.render.item_text`). A user
    whose old guidelines overflow the spec (>30, SpecInvalid) gets those guideline texts instead,
    so neither ever fails on it."""
    try:
        spec = effective_spec(state)
    except SpecInvalid:
        return tuple(g.text for g in state.guidelines)
    return tuple(item_text(i) for i in spec.items if i.authority != "pending"
                 and (domains or i.domain is None))


# ---- edits (each returns version + 1) ----

def _build(version: int, items: Iterable[SpecItem], exemplars: Iterable[Exemplar],
           params: RepParams, user_params: tuple[str, ...] = ()) -> TeachingSpec:
    items, exemplars = tuple(items), tuple(exemplars)
    if len(exemplars) > MAX_EXEMPLARS:
        raise SpecInvalid(TOO_MANY_EXEMPLARS, public_message="You can keep at most {limit} examples.",
                          code="example_limit", params={"limit": MAX_EXEMPLARS})
    if len({e.exemplar_id for e in exemplars}) != len(exemplars):
        raise SpecInvalid("范例 id 重复", public_message="Example IDs must be unique.", code="example_duplicate")
    try:
        return TeachingSpec(version=version, items=items, exemplars=exemplars, params=params,
                            user_params=user_params)
    except ValidationError as e:
        raise SpecInvalid(str(e)) from e


def _next(spec: TeachingSpec, *, items: Iterable[SpecItem] | None = None,
          exemplars: Iterable[Exemplar] | None = None,
          params: RepParams | None = None) -> TeachingSpec:
    return _build(spec.version + 1, spec.items if items is None else items,
                  spec.exemplars if exemplars is None else exemplars,
                  spec.params if params is None else params, spec.user_params)


def _text(text: str) -> str:
    t = text.strip()
    if not t:
        raise SpecInvalid("规则不能为空", public_message="A teaching rule cannot be empty.", code="rule_empty")
    if len(t) > MAX_ITEM_TEXT:
        raise SpecInvalid(f"一条规则最多 {MAX_ITEM_TEXT} 字", public_message="A teaching rule can contain at most {limit} characters.",
                          code="rule_too_long", params={"limit": MAX_ITEM_TEXT})
    return t


def _domain(domain: str | None) -> str | None:
    d = norm_domain(domain)
    if d is not None and len(d) > MAX_DOMAIN:
        raise SpecInvalid(f"领域最多 {MAX_DOMAIN} 字", public_message="A domain can contain at most {limit} characters.",
                          code="domain_too_long", params={"limit": MAX_DOMAIN})
    return d


def _item(**kw) -> SpecItem:
    try:
        return SpecItem(**kw)
    except ValidationError as e:
        raise SpecInvalid(str(e)) from e


def _index(spec: TeachingSpec, item_id: str) -> int:
    for n, i in enumerate(spec.items):
        if i.item_id == item_id:
            return n
    raise LookupError(f"没有这条规则：{item_id}")


def add_item(spec: TeachingSpec, *, item_id: str, text: str, scope: SpecScope = "any",
             domain: str | None = None, origin: Origin = "user_ui",
             authority: Authority = "user", origin_ref: str | None = None) -> TeachingSpec:
    """Appends one item (text stripped, domain stripped / empty → None) with its provenance
    (rule provenance contract §1.1; who may add what is `rules.authority.settle`'s check)."""
    new = _item(item_id=item_id, text=_text(text), scope=scope, domain=_domain(domain),
                origin=origin, authority=authority, origin_ref=origin_ref,
                rev_by="user" if authority != "rsi" else "rsi")
    return _next(spec, items=(*spec.items, new))


def edit_item(spec: TeachingSpec, item_id: str, *, text: str | None = None,
              scope: SpecScope | None = None, domain: str | None | _Keep = KEEP) -> TeachingSpec:
    """Changes the given fields; keeps id, provenance and helpful/harmful counts (the checkpoint,
    `rules.authority.settle`, stamps rev / rev_by and makes a user's edit the user's rule).
    `domain=None` clears the domain; leave it out to keep it."""
    n = _index(spec, item_id)
    old = spec.items[n]
    new = _item(**{**old.model_dump(),
                   "text": old.text if text is None else _text(text),
                   "scope": old.scope if scope is None else scope,
                   "domain": old.domain if isinstance(domain, _Keep) else _domain(domain)})
    return _next(spec, items=(*spec.items[:n], new, *spec.items[n + 1:]))


def set_authority(spec: TeachingSpec, item_id: str, authority: Authority) -> TeachingSpec:
    """The item handed to `authority` (the user's 「设为我的规则」 / confirming a pending rule; the
    checkpoint refuses anything else)."""
    n = _index(spec, item_id)
    new = _item(**{**spec.items[n].model_dump(), "authority": authority})
    return _next(spec, items=(*spec.items[:n], new, *spec.items[n + 1:]))


def replace_item(spec: TeachingSpec, item: SpecItem) -> TeachingSpec:
    """The item with `item.item_id` replaced by `item` as it is (source, counts and all): the undo
    of an edit."""
    n = _index(spec, item.item_id)
    return _next(spec, items=(*spec.items[:n], item, *spec.items[n + 1:]))


def remove_item(spec: TeachingSpec, item_id: str) -> TeachingSpec:
    n = _index(spec, item_id)
    return _next(spec, items=(*spec.items[:n], *spec.items[n + 1:]))


def set_params(spec: TeachingSpec, params: RepParams) -> TeachingSpec:
    return _next(spec, params=params)


def add_exemplar(spec: TeachingSpec, exemplar: Exemplar) -> TeachingSpec:
    """4th exemplar → SpecInvalid."""
    if len(spec.exemplars) >= MAX_EXEMPLARS:
        raise SpecInvalid(TOO_MANY_EXEMPLARS, public_message="You can keep at most {limit} examples.",
                          code="example_limit", params={"limit": MAX_EXEMPLARS})
    return _next(spec, exemplars=(*spec.exemplars, exemplar))


def remove_exemplar(spec: TeachingSpec, exemplar_id: str) -> TeachingSpec:
    if all(e.exemplar_id != exemplar_id for e in spec.exemplars):
        raise LookupError(f"没有这个范例：{exemplar_id}")
    return _next(spec, exemplars=tuple(e for e in spec.exemplars if e.exemplar_id != exemplar_id))


def restore(current: TeachingSpec, old: TeachingSpec) -> TeachingSpec:
    """A new version (current + 1) with the old version's items, exemplars and params."""
    return _build(current.version + 1, old.items, old.exemplars, old.params, current.user_params)


def append_imported(spec: TeachingSpec, items: Iterable[SpecItem]) -> TeachingSpec:
    """Appends items as one version, no item count cap (the seed's rules, `teach_seed`)."""
    items = tuple(items)
    return _next(spec, items=(*spec.items, *items))


# ---- the write checkpoint (rule provenance contract §1.3) ----

@dataclass(frozen=True)
class AddRule:
    item_id: str
    text: str
    scope: SpecScope = "any"
    domain: str | None = None
    origin: Origin = "user_ui"
    authority: Authority = "user"
    origin_ref: str | None = None


@dataclass(frozen=True)
class EditRule:
    item_id: str
    text: str | None = None
    scope: SpecScope | None = None
    domain: str | None | _Keep = KEEP


@dataclass(frozen=True)
class RemoveRule:
    item_id: str


@dataclass(frozen=True)
class TakeOver:
    """「设为我的规则」 (an RSI rule) or confirming a pending one: authority → user."""

    item_id: str


@dataclass(frozen=True)
class ReplaceRule:
    """A newer rule supersedes `old_id` in one version: `new` takes the old item's place and the
    old item goes (the checkpoint records it `revoked`; its provenance and evidence stay in the
    older versions and in `WikiState.item_evidence`)."""

    old_id: str
    new: AddRule


RuleOp = AddRule | EditRule | RemoveRule | TakeOver | ReplaceRule


def write(spec: TeachingSpec, op: RuleOp, *, actor: Actor,
          decisions: Decisions = NO_DECISIONS) -> TeachingSpec:
    """One rule op as the next version, through the checkpoint (`rules.authority.settle`):
    RuleAuthorityError when `actor` may not do it. `commit_spec` checks the saved spec again, item
    by item, whatever produced it."""
    match op:
        case AddRule():
            new = add_item(spec, item_id=op.item_id, text=op.text, scope=op.scope,
                           domain=op.domain, origin=op.origin, authority=op.authority,
                           origin_ref=op.origin_ref)
        case EditRule():
            new = edit_item(spec, op.item_id, text=op.text, scope=op.scope, domain=op.domain)
        case RemoveRule():
            new = remove_item(spec, op.item_id)
        case TakeOver():
            new = set_authority(spec, op.item_id, "user")
        case ReplaceRule():
            n, a = _index(spec, op.old_id), op.new
            item = _item(item_id=a.item_id, text=_text(a.text), scope=a.scope,
                         domain=_domain(a.domain), origin=a.origin, authority=a.authority,
                         origin_ref=a.origin_ref, rev_by="user" if a.authority != "rsi" else "rsi")
            new = _next(spec, items=(*spec.items[:n], item, *spec.items[n + 1:]))
    return check(spec, new, actor=actor, decisions=decisions)
