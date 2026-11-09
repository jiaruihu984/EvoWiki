"""Teaching spec θ_rep,u flows over a `Repo` (R1): read the effective spec and its versions, and
save changes — items, params, exemplars, restore — each as ONE whole new version
(`teaching_spec_saved`, version = previous + 1; the reducer does not check, so `commit_spec` does).

A user who never saved a spec reads the virtual v1 (`wikiapp.teachspec.effective_spec`: default items +
their old guidelines). Their first write commits v1 (action `migrated`, with the feedback
guidelines' evidence) and then the change as v2, in one commit. Every write takes an optional
`base_version`: when it is not the current (effective) version → `WikiConflict` (409), nothing
written. Caps → `SpecInvalid` (422); unknown item / exemplar / version / page → LookupError (404).
Full old versions are read back from the event log (`Repo.wiki_events_of`), never kept in state."""

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from evowiki.rules.authority import Actor, settle
from evowiki.rules.decisions import Decisions
from evowiki.teach.contracts import Exemplar, RepParams, TeachingSpec
from evowiki.wikiapp import teachspec as tspec
from evowiki.wikiapp.events import (
    DecisionVia,
    PageRecord,
    SpecAction,
    TeachingSpecSaved,
    WikiEvent,
    WikiEventBody,
)
from evowiki.wikiapp.kwrite import Ids
from evowiki.wikiapp.repo import Commit, Repo, Snapshot, WikiConflict
from evowiki.wikiapp.state import WikiState
from evowiki.wikiapp.wiki_schemas import (
    ExemplarIn,
    RuleDecisionOut,
    SpecItemIn,
    SpecItemPatchIn,
    TeachSpecOut,
    TeachVersionOut,
    TeachVersionsOut,
    WikiPageExemplarIn,
)

SPEC_EVENT = "teaching_spec_saved"
MAX_EXEMPLAR_TEXT = 60_000
MAX_EXEMPLAR_TITLE = 200
NOT_ORGANIZED = "这一页还没整理好，不能做范例"


def _wev(new_id: Ids, now: datetime, body: WikiEventBody) -> WikiEvent:
    return WikiEvent(event_id=new_id("wev"), ts=now, body=body)


def stale_detail(current: int) -> str:
    return f"教学规范已经是第 {current} 版，请刷新后再改"


# ---- read ----

def spec_out(spec: TeachingSpec, state: WikiState, *, stored: bool) -> TeachSpecOut:
    ids = {i.item_id for i in spec.items}
    decided = [RuleDecisionOut(decision_id=d.decision_id, rule_id=d.rule_id, text=d.text,
                               action=d.action, via=d.via)
               for d in reversed(Decisions.of(state).active())
               if d.rule_id not in ids and d.action in ("rejected", "revoked")]
    return TeachSpecOut(version=spec.version, stored=stored, items=list(spec.items),
                        exemplars=list(spec.exemplars), params=spec.params,
                        evidence={k: v for k, v in state.item_evidence.items() if k in ids},
                        decisions=decided)


def get_spec(state: WikiState) -> TeachSpecOut:
    """The effective spec (the virtual v1 when nothing is stored; >30 old guidelines → 422)."""
    return spec_out(tspec.effective_spec(state), state, stored=state.spec is not None)


def list_versions(state: WikiState) -> TeachVersionsOut:
    return TeachVersionsOut(
        current=tspec.effective_version(state),
        items=[TeachVersionOut(**m.model_dump()) for m in state.spec_versions])


def read_version(repo: Repo, snap: Snapshot, version: int) -> TeachingSpec:
    """A full old version, read back from the event log (the last save of that number wins).
    Nothing stored and version 1 → the virtual v1. Unknown → LookupError."""
    if snap.state.spec is None:
        if version == 1:
            return tspec.effective_spec(snap.state)
        raise LookupError(f"没有第 {version} 版教学规范")
    if all(m.version != version for m in snap.state.spec_versions):
        raise LookupError(f"没有第 {version} 版教学规范")
    found: TeachingSpec | None = None
    for ev in repo.wiki_events_of(snap.user_id, (SPEC_EVENT,)):
        b = ev.body
        if isinstance(b, TeachingSpecSaved) and b.spec.version == version:
            found = b.spec
    if found is None:
        raise LookupError(f"没有第 {version} 版教学规范")
    return found


def version_out(repo: Repo, user_id: str, version: int) -> TeachSpecOut:
    snap = repo.load(user_id)
    return spec_out(read_version(repo, snap, version), snap.state,
                    stored=snap.state.spec is not None)


# ---- write ----

def commit_spec(repo: Repo, snap: Snapshot, change: Callable[[TeachingSpec], TeachingSpec], *,
                actor: Actor, action: SpecAction, now: datetime, new_id: Ids,
                via: DecisionVia = "ui", undo: bool = False, origin: str | None = None,
                evidence: Mapping[str, str] | None = None,
                extra: Sequence[WikiEventBody] = (),
                base_version: int | None = None,
                commit_extra: Sequence[Callable[[Any], None]] = ()) -> Snapshot:
    """Saves `change(current)` as the next version. `base_version` must equal the effective
    version (else WikiConflict); a user with no stored spec first gets v1 = the migration
    (action `migrated`), in the same commit. The new version must be exactly previous + 1
    (RuntimeError otherwise — a flow bug, nothing written). `extra` events land after the save
    in the same commit. `commit_extra`: writes that land in that commit (`Commit.extra`: an action
    ledger's effect). Returns the committed snapshot.

    `actor` (mandatory: rule provenance contract §1.3) is who makes the change: the saved spec is
    checked against the one it came from, item by item (`rules.authority.settle`:
    RuleAuthorityError, nothing written, when the actor may not make it), its provenance is
    stamped, and the user's rule decisions it implies (`RuleDecided`, `via`, ref = `origin`) land in
    the same commit. `undo`: the user undoing an RSI change (a restored item keeps its authority)."""
    state = snap.state
    current = tspec.effective_version(state)
    if base_version is not None and base_version != current:
        raise WikiConflict(stale_detail(current), public_message="The teaching preferences are now at version {version}. Refresh before editing.",
                           code="teaching_spec_conflict", params={"version": current})
    bodies: list[WikiEventBody] = []
    if state.spec is None:
        base = tspec.effective_spec(state)
        bodies.append(TeachingSpecSaved(spec=base, action="migrated",
                                        evidence=tspec.migration_evidence(state.guidelines)))
    else:
        base = state.spec
    new = change(base)
    if new.version != base.version + 1:
        raise RuntimeError(f"spec version {new.version} after {base.version}")
    new, decided = settle(base, new, actor=actor, decisions=Decisions.of(state), via=via,
                          ref=origin, undo=undo, new_id=new_id)
    bodies.append(TeachingSpecSaved(spec=new, action=action, origin=origin,
                                    evidence=dict(evidence or {})))
    bodies.extend(decided)
    bodies.extend(extra)
    return repo.commit(snap, Commit(wiki=[_wev(new_id, now, b) for b in bodies],
                                    extra=list(commit_extra)))


def _saved(snap: Snapshot) -> TeachSpecOut:
    assert snap.state.spec is not None
    return spec_out(snap.state.spec, snap.state, stored=True)


def add_item_flow(repo: Repo, user_id: str, body: SpecItemIn, *, now: datetime, new_id: Ids,
                  base_version: int | None = None) -> TeachSpecOut:
    """A user item (source `user`, id `si…`) appended at the end; 31st → SpecInvalid."""
    item_id = new_id("si")
    return _saved(commit_spec(
        repo, repo.load(user_id),
        lambda s: tspec.write(s, tspec.AddRule(item_id, body.text, body.scope, body.domain),
                              actor="user"),
        actor="user", action="edit", now=now, new_id=new_id, base_version=base_version))


def edit_item_flow(repo: Repo, user_id: str, item_id: str, body: SpecItemPatchIn, *,
                   now: datetime, new_id: Ids, base_version: int | None = None) -> TeachSpecOut:
    """Changes only the fields sent; the item becomes the user's (authority `user`, origin kept,
    rev + 1; keeps id and counts)."""
    sent = body.model_fields_set
    domain = body.domain if "domain" in sent else tspec.KEEP
    return _saved(commit_spec(
        repo, repo.load(user_id),
        lambda s: tspec.write(s, tspec.EditRule(item_id, body.text, body.scope, domain),
                              actor="user"),
        actor="user", action="edit", now=now, new_id=new_id, base_version=base_version))


def remove_item_flow(repo: Repo, user_id: str, item_id: str, *, now: datetime, new_id: Ids,
                     base_version: int | None = None) -> TeachSpecOut:
    """The user deletes a rule: `revoked` is recorded (RSI never adds that version back; a similar
    rule is not banned, and the user may add it again). A pending rule's waiting card closes with
    it, else the assistant keeps naming it as waiting."""
    from evowiki.wikiapp.preferences import close_pending  # preferences imports this module

    return _saved(commit_spec(repo, repo.load(user_id),
                              lambda s: tspec.write(s, tspec.RemoveRule(item_id), actor="user"),
                              actor="user", action="edit", now=now, new_id=new_id,
                              base_version=base_version,
                              commit_extra=[close_pending(user_id, item_id, now)]))


def take_over_flow(repo: Repo, user_id: str, item_id: str, *, now: datetime, new_id: Ids,
                   base_version: int | None = None) -> TeachSpecOut:
    """「设为我的规则」 (decision 2): an RSI or pending rule becomes the user's without an edit —
    origin unchanged, authority `user`, a `taken_over` (for a pending rule: `reaccepted`) decision
    recorded; RSI can no longer change or remove it. A rule already the user's → SpecInvalid."""
    snap = repo.load(user_id)
    cur = next((i for i in tspec.effective_spec(snap.state).items if i.item_id == item_id), None)
    if cur is None:
        raise LookupError(f"没有这条规则：{item_id}")
    if cur.authority == "user":
        raise tspec.SpecInvalid("这条已经是你的规则了")
    return _saved(commit_spec(repo, snap,
                              lambda s: tspec.write(s, tspec.TakeOver(item_id), actor="user"),
                              actor="user", action="edit", now=now, new_id=new_id,
                              base_version=base_version))


def set_params_flow(repo: Repo, user_id: str, params: RepParams, *, now: datetime, new_id: Ids,
                    base_version: int | None = None) -> TeachSpecOut:
    return _saved(commit_spec(repo, repo.load(user_id),
                              lambda s: tspec.set_params(s, params),
                              actor="user", action="params", now=now, new_id=new_id,
                              base_version=base_version))


def restore_flow(repo: Repo, user_id: str, version: int, *, now: datetime, new_id: Ids,
                 base_version: int | None = None) -> TeachSpecOut:
    """A new version with version `version`'s items, exemplars and params (read from the log);
    action `restore`, origin `v{n}`. The user's own action: an item that comes back keeps its
    origin; one the user had deleted or rejected comes back with a `reaccepted` decision.
    Unknown version → LookupError."""
    snap = repo.load(user_id)
    old = read_version(repo, snap, version)
    return _saved(commit_spec(repo, snap, lambda s: tspec.restore(s, old),
                              actor="user", action="restore", origin=f"v{version}", now=now, new_id=new_id,
                              base_version=base_version))


def page_markdown(rec: PageRecord) -> str:
    """An organized page as Markdown: `# title`, then per outline section `## heading`, the cue
    as a quote, the body (a check section without text: its stem and options)."""
    assert rec.page is not None
    parts = [f"# {rec.title.strip()}"]
    for s in rec.page.outline.sections:
        block = [f"## {s.heading.strip()}"]
        t = rec.page.texts.get(s.id)
        if t is not None and t.cue.strip():
            block.append(f"> {t.cue.strip()}")
        if t is not None and t.body.strip():
            block.append(t.body.strip())
        elif s.check is not None:
            block.append("\n".join([s.check.stem.strip(), *(f"- {o}" for o in s.check.options)]))
        parts.append("\n\n".join(block))
    return "\n\n".join(parts) + "\n"


def page_exemplar(state: WikiState, page_id: str, exemplar_id: str) -> Exemplar:
    """Unknown page → LookupError; not organized / over 60_000 characters → SpecInvalid."""
    rec = state.pages.get(page_id)
    if rec is None:
        raise LookupError(f"没有这一页：{page_id}")
    if not rec.has_body:              # not organized, or still being written
        raise tspec.SpecInvalid(NOT_ORGANIZED, public_message="This page is not finished yet and cannot be used as an example.",
                                code="example_page_unfinished")
    text = page_markdown(rec)
    if len(text) > MAX_EXEMPLAR_TEXT:
        raise tspec.SpecInvalid(f"范例最多 {MAX_EXEMPLAR_TEXT} 字，这一页有 {len(text)} 字")
    title = rec.title.strip()[:MAX_EXEMPLAR_TITLE] or page_id[:MAX_EXEMPLAR_TITLE]
    return Exemplar(exemplar_id=exemplar_id, kind="wiki_page", ref=page_id, title=title,
                    text=text)


def add_exemplar_flow(repo: Repo, user_id: str, body: ExemplarIn, *, now: datetime,
                      new_id: Ids, base_version: int | None = None,
                      exemplar_id: str | None = None,
                      commit_extra: Sequence[Callable[[Any], None]] = ()) -> TeachSpecOut:
    """`wiki_page`: the server snapshots the organized page; `vault_note`: the uploaded snapshot.
    A 4th exemplar → SpecInvalid. `exemplar_id` (default: a new one) and `commit_extra` (writes in
    the same commit) are for the assistant's `set_exemplar` (ledger effect + undo data)."""
    snap = repo.load(user_id)
    exemplar_id = exemplar_id or new_id("ex")
    if isinstance(body, WikiPageExemplarIn):
        ex = page_exemplar(snap.state, body.ref, exemplar_id)
    else:
        ex = Exemplar(exemplar_id=exemplar_id, kind="vault_note", ref=body.ref,
                      title=body.title, text=body.text)
    return _saved(commit_spec(repo, snap, lambda s: tspec.add_exemplar(s, ex),
                              actor="user", action="exemplar", now=now, new_id=new_id,
                              base_version=base_version, commit_extra=commit_extra))


def remove_exemplar_flow(repo: Repo, user_id: str, exemplar_id: str, *, now: datetime,
                         new_id: Ids, base_version: int | None = None,
                         commit_extra: Sequence[Callable[[Any], None]] = ()) -> TeachSpecOut:
    return _saved(commit_spec(repo, repo.load(user_id),
                              lambda s: tspec.remove_exemplar(s, exemplar_id),
                              actor="user", action="exemplar", now=now, new_id=new_id,
                              base_version=base_version, commit_extra=commit_extra))
