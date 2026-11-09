"""Replay cases for the harness-RSI loop (design 2026-10-21 §4): the sections behind the biggest group
of feedback signals ("problem" cases) and one good section per exemplar page ("anchor" cases), each
tied to the deep write run and snapshot it is replayed from.

Sources: the signals are given; their grouping is `teach.localize` (section logs from the repo);
the runs are `store.trace.deep_runs_for_page`. The page a run wrote against is the one it was given
(its traced `page`: a first write's fresh outline, a feedback outline) or else the stored page of its
snapshot (`Repo.load_at` at the run's seqs, the one exact source since the logs are append-only);
whether the section exists, and how long it is, comes from that page. A run traced before the
`page` key existed is not replayed when it was a first write or a feedback rewrite (its outline was
never recorded).

A section the writer added during the run (or one the run's input had without a text) is in no
run's input page. Its case comes from the page as the run's job saved it: the job's commit in the
wiki log, i.e. the first `page_saved` of the page after the run's snapshot whose `WriteMark.job_id`
is the job's (the run's traced `job_id`, else its run id prefix before ":write:", which is the job
id of the page workflow's own writes) and is that job's own new mark (not the page's mark before
it: a later save that only copies an earlier commit's mark is not this run's), read back with
`Repo.load_at` at that commit's seq. That is
exact: the logs are append-only and the commit stored that page. The trace's tool steps are not (the
commit fills placeholders and drops interactives that never passed). Such a case is a rewrite of
that section (mode "rewrite", no instruction) on the run's own snapshot with the saved page as its
`page`, and names the commit (`Case.page_seq`), so the reference and the candidate replay both get
exactly that input. The same holds for a section a scoped run (a refine, a feedback rewrite of
other sections) inserted: it is outside the run's traced scope but in its job's saved
`WriteMark.sections`. A page revision approved from the Bot is saved under `revise:<action id>`,
which names no write run, so a section it saved has its own reason. Every other way a signal or an exemplar gives no case has its own reason (the
module constants). Nothing is written.

Assistant cases (harness 3b follow-up 3, `assistant_cases` + `with_assistant`): the round's
`assistant_rejected` and `assistant_rated` (👎) signals (`wikiapp.signals.assistant_feedback`, each
with its turn's run id) give one case per turn (a turn both rejected and rated down is one case) (kind "assistant", case id "turn:<run id>", its signals in it; turns with
more signals first). A turn is checked before it becomes a case (`replay_assistant.check_turn`: the
trace is there, its system prompt and tools can be rebuilt, the replay routes to its model); a
turn that fails a check is unusable, each of its signals listed with the plain reason, and never
takes a slot. The problem cases of both kinds share MAX_PROBLEM_CASES: deep and assistant cases
alternate (deep first, each kind in its own order) until the cap, so neither kind crowds the other
out and a kind that runs short leaves its slots to the other. There are no assistant anchors (no
exemplar turn exists); the deep exemplar anchors stay only while a deep problem case is in the set.
Cases the shared cap leaves out, of either kind, are dropped silently (no unusable row), as deep
cases over the cap always were. Every problem case counts toward the round's MIN_CASES check
before the improver runs; the final probe counts only the cases of the kinds the change touches
(`round`, step 4)."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

import psycopg

from evowiki.agent.trace import NotReplayable
from evowiki.deep.agent import SnapshotSeqs
from evowiki.goals.contracts import SectionRole
from evowiki.goals.fold import Page
from evowiki.harness.anchors import MAX_EXEMPLARS, MAX_PROBLEM_CASES
from evowiki.harness.observe import JOB_SEP
from evowiki.harness.pack import HarnessInvalid
from evowiki.harness.replay import NO_SEQS, Case
from evowiki.harness.replay_assistant import ReplaySourceMissing
from evowiki.store.trace import deep_runs_for_page
from evowiki.teach.contracts import Signal, TeachingSpec
from evowiki.teach.localize import CLASS_ZH, Finding, localize
from evowiki.wikiapp.repo import Repo
from evowiki.wikiapp.spec_store import RepoSectionLogs

RUNS_SEEN = 5                  # newest runs of a page looked at per signal
FINISHED = "finished"          # a write run that reached its finish call

# why a signal (or an exemplar page) gives no case: each is its own cause
NO_PAGE_SIGNAL = "反馈没有落在某一页上"
NO_COMPLAINT_TARGET = "系统反馈没有指定可重放的回答，不能把当前页面当作出错来源"
NO_SECTION = "反馈没有落在某一段上"
UNPLACED = "这条反馈对应的写作记录已经不是当时的版本，归不到某一段"
NO_RUN = "找不到写这一段的记录"
NOT_FINISHED = "那几次写作都没有正常写完"
NO_SNAPSHOT = "那次写作没有留下当时的页面快照"
DRY_ONLY = "那几次写作都是试写，没有存进页面"
NOT_COVERED = "那几次写作都没有改到这一段"
NO_LOAD = "读不到那次写作时的页面"
NO_OUTLINE = "那次写作用的提纲没有记下来（较早的记录），不能照原样重放"
NO_PAGE = "那次写作时的快照里没有这一页"
CHECK_SECTION = "这一段是确认题，不是写作写的，不能重放"
NOT_SAVED = "这一段是那次写作中新加的（或者当时还没有正文），但那次写作的结果没有存进页面"
SAVED_NO_LOAD = "读不到那次写作存下的页面"
NOT_WRITTEN = "那次写作没有写这一段"
ADDED_AFTER = "这一段是那次写作之后才加上的，没有写它的写作记录"
SECTION_REMOVED = "这一段不在那次写作的输入和结果里，现在页面上也已经没有了"
REVISED = ("这一段后来是按资料改写、经你同意后存进页面的；那次存页面时没有记下是哪次写作，"
           "对不上写作记录，不能重放")
NO_EXEMPLAR_RUN = "这个范例页没有可用的写作记录"
TRACE_EXPIRED = "那几次写作的执行记录已过保留期，原文已清除，不能完整重放"
NO_EXEMPLAR_SECTION = "这个范例页那次写作没有写出能重放的段"
TURN_ATTACHMENTS = "这次回答发过附件，执行记录里没有附件内容，不能重放"
TURN_INCOMPLETE = "这次回答的执行记录不全，不能重放"
TURN_PACK = "现在的运行设置和这次回答对不上，不能重放"
ASSISTANT_FINDING = "助手的回答被你拒绝、撤回或点了踩"
EXEMPLAR = "exemplar:"         # the unusable id prefix of an exemplar page
REVISE_JOB = "revise:"         # the job id prefix of a Bot page revision's commit (bot.executors_revise)


@dataclass(frozen=True)
class CaseSet:
    problem: tuple[Case, ...]
    anchors: tuple[Case, ...]
    unusable: tuple[tuple[str, str], ...]     # (signal id | "exemplar:<page id>", plain-Chinese reason)
    finding: str                              # the chosen group in words; "" when there is none


@dataclass(frozen=True)
class _Saved:
    """The page as a run's job saved it (module doc): the commit's seq, its page, and the sections
    that job wrote (`WriteMark.sections`)."""

    seq: int
    page: Page
    sections: tuple[str, ...]


def select_cases(conn: psycopg.Connection, user_id: str, signals: Sequence[Signal],
                 spec: TeachingSpec, *, now: datetime, repo: Repo) -> CaseSet:
    """`signals` grouped by `localize` (a one-signal group counts); the largest group is the finding
    (ties: B before A, then the failure name). Its signals each get the newest finished, non-dry-run
    deep run of their page before them that has a snapshot and covers their section; one case per
    (run, section), sections with more signals first, at most MAX_PROBLEM_CASES. A section the run's
    input page has not written is replayed from the page the run's job saved (module doc). Anchors:
    the longest section of each `wiki_page` exemplar that is not already a problem page, from that
    page's newest usable run before `now`, at most MAX_EXEMPLARS. `repo` loads the snapshots, the
    saved pages and the section logs."""
    pages = {s.page_id for s in signals if s.page_id}
    found = localize(signals, RepoSectionLogs(repo).section_logs(user_id, pages), spec, min_signals=1)
    chosen = _largest(found.b + found.a)
    unusable: list[tuple[str, str]] = [(i, UNPLACED) for i in found.unplaced]
    unusable.extend((s.signal_id, NO_COMPLAINT_TARGET) for s in signals if s.kind == 'user_complaint')
    problem = (_problem(conn, user_id, _on_sections(chosen), repo, unusable) if chosen else ())
    anchor = _anchors(conn, user_id, spec, repo, now, {c.page_id for c in problem}, unusable)
    return CaseSet(problem, anchor, tuple(unusable), _describe(chosen, repo, user_id))


def _on_sections(f: Finding) -> tuple[Signal, ...]:
    """A signal on a whole page takes the first section localization traced it to on that page."""
    first: dict[str, str] = {}
    for page, section in f.sections:
        first.setdefault(page, section)
    return tuple(s if s.section_id is not None or s.page_id not in first
                 else s.model_copy(update={"section_id": first[s.page_id]}) for s in f.signals)


def _largest(findings: Sequence[Finding]) -> Finding | None:
    ranked = sorted(findings, key=lambda f: (-len(f.signals), f.localization.track,
                                             f.localization.failure))
    return ranked[0] if ranked else None


def _describe(f: Finding | None, repo: Repo, user_id: str) -> str:
    if f is None:
        return ""
    pages = repo.load(user_id).state.pages
    titles = sorted({pages[p].title if p in pages else p for p, _ in f.sections})
    return f"{CLASS_ZH.get(f.localization.failure, f.localization.failure)}：{'、'.join(titles)}"


def _covers(run: dict, section_id: str) -> bool:
    scope = run["input"].get("scope")
    return scope is None or not scope.get("sections") or section_id in scope["sections"]


def _usable(run: dict) -> bool:
    inp = run["input"]
    return (run["stop"] == FINISHED and inp.get("snapshot") is not None
            and not (inp.get("scope") or {}).get("dry_run"))


def _case(run: dict, user_id: str, page_id: str, section_id: str, signal_ids: tuple[str, ...],
          anchor: bool, saved: _Saved | None = None) -> Case:
    """The run's own input; with `saved`, a rewrite of the section on the page its job saved."""
    inp = run["input"]
    extra = {"domain": inp["domain"]} if "domain" in inp else {}
    if saved is None:
        how = {"mode": inp.get("mode", "rewrite"), "instruction": inp.get("instruction", ""),
               "page": inp.get("page")}
    else:
        how = {"mode": "rewrite", "instruction": "", "page": saved.page.model_dump(mode="json"),
               "page_seq": saved.seq}
    return Case(case_id=f"{'anchor' if anchor else 'case'}:{run['run_id']}:{section_id}",
                run_id=run["run_id"], page_id=page_id, section_id=section_id, signal_ids=signal_ids,
                anchor=anchor, snapshot=SnapshotSeqs(**inp["snapshot"]), user_id=user_id,
                **how, **extra)


def _source(repo: Repo, user_id: str, run: dict, page_id: str) -> tuple[Page | None, str]:
    """The page the run wrote against (module doc), or (None, the plain-Chinese reason)."""
    inp = run["input"]
    s = inp["snapshot"]
    try:
        snap = repo.load_at(user_id, s["wiki_seq"], s["aspect_seq"], s["infer_version"])
    except LookupError:
        return None, NO_LOAD
    rec = snap.state.pages.get(page_id)
    if rec is None:
        return None, NO_PAGE
    if inp.get("page") is not None:
        return Page.model_validate(inp["page"]), ""
    if "page" not in inp and inp.get("mode") in ("page", "feedback"):
        return None, NO_OUTLINE                  # traced before the key: its outline is unknown
    return (rec.page, "") if rec.page is not None else (None, NO_PAGE)


def _written(page: Page, section_id: str, mode: str) -> bool:
    """The section exists in the page the run wrote against and has a text there, or the run is a
    first write (mode "page": it writes every non-check section of its fresh outline)."""
    s = next((x for x in page.outline.sections if x.id == section_id), None)
    if s is None:
        return False
    return section_id in page.texts or (mode == "page" and s.role is not SectionRole.CHECK)


def _job_ids(run: dict) -> set[str]:
    """The ids the WriteMark of the run's job can carry (module doc)."""
    ids = {run["run_id"].split(JOB_SEP)[0]}
    if run["input"].get("job_id"):
        ids.add(run["input"]["job_id"])
    return ids


def _saved(repo: Repo, user_id: str, run: dict, page_id: str) -> tuple[_Saved | None, str]:
    """The page as the run's job saved it (module doc); (None, "") when the job saved nothing,
    (None, SAVED_NO_LOAD) when that commit cannot be read back."""
    s = run["input"]["snapshot"]
    ids = _job_ids(run)
    try:
        before = repo.load_at(user_id, s["wiki_seq"], s["aspect_seq"],
                              s["infer_version"]).state.pages.get(page_id)
    except LookupError:
        return None, SAVED_NO_LOAD
    prev = before.written if before is not None else None
    seq = None
    for q, ev in repo.wiki_events_since(user_id, {"page_saved"}, after_seq=s["wiki_seq"]):
        if ev.body.record.page_id != page_id:
            continue
        mark = ev.body.record.written
        if mark is not None and mark.job_id in ids and mark != prev:    # the job's own new mark
            seq = q
            break
        prev = mark
    if seq is None:
        return None, ""
    try:
        rec = repo.load_at(user_id, seq, s["aspect_seq"], s["infer_version"]).state.pages.get(
            page_id)
    except LookupError:
        return None, SAVED_NO_LOAD
    if rec is None or rec.page is None or rec.written is None:
        return None, SAVED_NO_LOAD
    return _Saved(seq=seq, page=rec.page, sections=rec.written.sections), ""


def _revised(repo: Repo, user_id: str, page_id: str, section_id: str, after_seq: int) -> bool:
    """A Bot page revision saved after `after_seq` wrote `section_id` of the page."""
    return any(ev.body.record.page_id == page_id and ev.body.record.written is not None
               and ev.body.record.written.job_id.startswith(REVISE_JOB)
               and section_id in ev.body.record.written.sections
               for _, ev in repo.wiki_events_since(user_id, {"page_saved"}, after_seq=after_seq))


def _added(repo: Repo, user_id: str, run: dict, page: Page, page_id: str,
           section_id: str) -> tuple[_Saved | None, str]:
    """A section the run's input page `page` has not written: the page the run's job saved when
    that job wrote the section there, else (None, the reason)."""
    sec = next((x for x in page.outline.sections if x.id == section_id), None)
    if sec is not None and sec.role is SectionRole.CHECK:
        return None, CHECK_SECTION
    saved, why = _saved(repo, user_id, run, page_id)
    if saved is None and not why and _revised(repo, user_id, page_id, section_id,
                                              run["input"]["snapshot"]["wiki_seq"]):
        return None, REVISED
    if saved is None:
        return None, why or NOT_SAVED
    if section_id in saved.sections and section_id in saved.page.texts:
        return saved, ""
    if sec is not None or section_id in {x.id for x in saved.page.outline.sections}:
        return None, NOT_WRITTEN
    rec = repo.load(user_id).state.pages.get(page_id)
    now = {x.id for x in rec.page.outline.sections} if rec is not None and rec.page else set()
    return None, ADDED_AFTER if section_id in now else SECTION_REMOVED


def _problem(conn, user_id: str, signals: Sequence[Signal], repo: Repo,
             unusable: list[tuple[str, str]]) -> tuple[Case, ...]:
    groups: dict[tuple[str, str], list[str]] = {}          # (run id, section) -> signal ids
    runs: dict[str, dict] = {}
    pages: dict[str, str] = {}
    saved: dict[tuple[str, str], _Saved] = {}              # (run id, section) -> the saved page
    for sig in signals:
        if sig.page_id is None or sig.section_id is None:
            unusable.append((sig.signal_id, NO_PAGE_SIGNAL if sig.page_id is None else NO_SECTION))
            continue
        run, why = _run_for(conn, user_id, sig, repo)
        if run is None:
            unusable.append((sig.signal_id, why))
            continue
        page, why = _source(repo, user_id, run, sig.page_id)
        if page is None:
            unusable.append((sig.signal_id, why))
            continue
        key = (run["run_id"], sig.section_id)
        if (not _covers(run, sig.section_id)          # a section the scoped run inserted
                or not _written(page, sig.section_id, run["input"].get("mode", "rewrite"))):
            out, why = _added(repo, user_id, run, page, sig.page_id, sig.section_id)
            if out is None:
                unusable.append((sig.signal_id, why))
                continue
            saved[key] = out
        groups.setdefault(key, []).append(sig.signal_id)
        runs[run["run_id"]], pages[run["run_id"]] = run, sig.page_id
    order = sorted(groups, key=lambda k: -len(groups[k]))       # stable: first seen wins a tie
    return tuple(_case(runs[rid], user_id, pages[rid], sid, tuple(groups[rid, sid]), False,
                       saved.get((rid, sid)))
                 for rid, sid in order[:MAX_PROBLEM_CASES])


def _run_for(conn, user_id: str, sig: Signal, repo: Repo) -> tuple[dict | None, str]:
    """The newest usable run whose traced scope covers the signal's section, else the newest usable
    run whose job saved that section (a section a scoped run inserted), else (None, the reason)."""
    seen = deep_runs_for_page(conn, user_id, sig.page_id, before=sig.at, limit=RUNS_SEEN)
    rows = [r for r in seen if r["stop"] == FINISHED]
    if not rows:
        return None, NOT_FINISHED if seen else NO_RUN
    rows = [r for r in rows if r["text_expired_at"] is None]   # store.retention emptied the rest
    if not rows:
        return None, TRACE_EXPIRED
    snapped = [r for r in rows if r["input"].get("snapshot") is not None]
    if not snapped:
        return None, NO_SNAPSHOT
    live = [r for r in snapped if _usable(r)]
    if not live:
        first = min(r["input"]["snapshot"]["wiki_seq"] for r in snapped)
        revised = _revised(repo, user_id, sig.page_id, sig.section_id, first)
        return None, REVISED if revised else DRY_ONLY
    run = next((r for r in live if _covers(r, sig.section_id)), None)
    if run is not None:
        return run, ""
    unread = False
    for r in live:
        saved, why = _saved(repo, user_id, r, sig.page_id)
        unread = unread or bool(why)
        if (saved is not None and sig.section_id in saved.sections
                and sig.section_id in saved.page.texts):
            return r, ""
    return None, SAVED_NO_LOAD if unread else NOT_COVERED


def _anchors(conn, user_id: str, spec: TeachingSpec, repo: Repo, now: datetime, skip: set[str],
             unusable: list[tuple[str, str]]) -> tuple[Case, ...]:
    out: list[Case] = []
    for ex in spec.exemplars:
        if ex.kind != "wiki_page" or ex.ref in skip:
            continue
        if len(out) >= MAX_EXEMPLARS:
            break
        key = f"{EXEMPLAR}{ex.ref}"
        seen = deep_runs_for_page(conn, user_id, ex.ref, before=now, limit=RUNS_SEEN)
        run = next((r for r in seen if r["text_expired_at"] is None and _usable(r)), None)
        if run is None:
            expired = any(r["text_expired_at"] is not None for r in seen)
            unusable.append((key, TRACE_EXPIRED if expired else NO_EXEMPLAR_RUN))
            continue
        page, why = _source(repo, user_id, run, ex.ref)
        if page is None:
            unusable.append((key, why))
            continue
        saved, why = _saved(repo, user_id, run, ex.ref)
        if why:                        # fail closed: never measured without the page its job saved
            unusable.append((key, why))
            continue
        mode = run["input"].get("mode", "rewrite")
        lengths = _lengths(repo, user_id, ex.ref, page, mode, saved)
        if not lengths:
            unusable.append((key, NO_EXEMPLAR_SECTION))
            continue
        longest = max(lengths, key=lambda s: lengths[s])
        added = None if _written(page, longest, mode) else saved
        out.append(_case(run, user_id, ex.ref, longest, (), True, added))
    return tuple(out)


def _lengths(repo: Repo, user_id: str, page_id: str, page: Page, mode: str,
             saved: _Saved | None) -> dict[str, int]:
    """Body length of each section the run could replay: the ones its input `page` has written
    and the ones its job wrote into the saved page. Measured by the text the job saved; without a
    saved page, by the input text, or for a first write (no texts yet) by what is stored now (what
    that write produced, unless rewritten since)."""
    ids = [s.id for s in page.outline.sections if _written(page, s.id, mode)]
    if saved is not None:
        ids += [s for s in saved.sections if s not in ids and s in saved.page.texts]
        texts = saved.page.texts
    else:
        texts = page.texts
        if not texts and mode == "page":
            rec = repo.load(user_id).state.pages.get(page_id)
            texts = rec.page.texts if rec is not None and rec.page is not None else {}
    return {s: len(texts[s].body) for s in ids if s in texts}


# --- assistant cases (module doc) -------------------------------------------------------------

def assistant_cases(rejections: Sequence[tuple[Signal, str]], user_id: str, *,
                    check: Callable[[Case], None]
                    ) -> tuple[tuple[Case, ...], tuple[tuple[str, str], ...]]:
    """(the usable turn cases, (signal id, reason) of every signal whose turn is unusable) from
    `rejections` (signal, the turn's run id). `check(case)` raises what `check_turn` raises."""
    groups: dict[str, list[str]] = {}
    for sig, run_id in rejections:
        ids = groups.setdefault(run_id, [])
        if sig.signal_id not in ids:
            ids.append(sig.signal_id)
    cases: list[Case] = []
    unusable: list[tuple[str, str]] = []
    for run_id in sorted(groups, key=lambda r: -len(groups[r])):      # stable: first seen first
        case = Case(case_id=f"turn:{run_id}", run_id=run_id, page_id="", section_id="",
                    signal_ids=tuple(groups[run_id]), anchor=False, snapshot=NO_SEQS,
                    mode="rewrite", instruction="", user_id=user_id, kind="assistant")
        why = _turn_unusable(case, check)
        if why:
            unusable += [(sid, why) for sid in groups[run_id]]
        else:
            cases.append(case)
    return tuple(cases), tuple(unusable)


def _turn_unusable(case: Case, check: Callable[[Case], None]) -> str:
    """"" when the turn can be replayed, else the plain reason. Any other error is raised (a
    store failure fails the round closed)."""
    try:
        check(case)
    except ReplaySourceMissing as e:
        return e.reason
    except NotReplayable:
        return TURN_ATTACHMENTS
    except LookupError:                    # no model step, a pack version missing from history
        return TURN_INCOMPLETE
    except HarnessInvalid:
        return TURN_PACK
    return ""


def with_assistant(cs: CaseSet, turns: Sequence[Case],
                   unusable: Sequence[tuple[str, str]]) -> CaseSet:
    """`cs` (the deep selection) with the turn cases and their unusable signals (module doc):
    problem cases alternate deep / assistant up to MAX_PROBLEM_CASES; without a deep problem
    case the exemplar anchors (and their unusable rows) are dropped; the finding names the
    assistant part. No turn and nothing unusable: `cs` itself."""
    if not turns and not unusable:
        return cs
    deep, asst = list(cs.problem), list(turns)
    problem: list[Case] = []
    while (deep or asst) and len(problem) < MAX_PROBLEM_CASES:
        for side in (deep, asst):
            if side and len(problem) < MAX_PROBLEM_CASES:
                problem.append(side.pop(0))
    has_deep = any(c.kind == "deep" for c in problem)
    anchors = cs.anchors if has_deep else ()
    left = cs.unusable if has_deep else tuple(u for u in cs.unusable
                                               if not u[0].startswith(EXEMPLAR))
    has_turn = any(c.kind == "assistant" for c in problem)
    finding = "；".join(f for f in (cs.finding, ASSISTANT_FINDING if has_turn else "") if f)
    return replace(cs, problem=tuple(problem), anchors=anchors,
                   unusable=(*left, *unusable), finding=finding)
