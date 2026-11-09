import argparse
import os
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from evowiki.content.models import Concept, Edge, QuizItem, Segment
from evowiki.llm.gateway import Gateway, MissingRecordingError, Provider
from evowiki.llm.providers import ENV_KEYS, missing_env_vars, providers_from_env
from evowiki.pipeline.chunking import ingest_registration
from evowiki.pipeline.config import PipelineConfig, load_config
from evowiki.pipeline.presumes import (
    PresumesRevisionError,
    propose_presumes_correction,
    propose_presumes_revision,
)
from evowiki.pipeline.report import StageReport
from evowiki.pipeline.review import ReviewState, ReviewTarget
from evowiki.pipeline.run import (
    UnknownConceptError,
    build_bundle,
    load_context,
    run_pipeline,
    snapshot_workspace,
)
from evowiki.pipeline.skeleton import DOMAINS, approved_concepts, expand_domain
from evowiki.pipeline.snapshot import SnapshotValidationError
from evowiki.pipeline.sources import (
    Fetcher,
    SourceRegistration,
    eligible_sources,
    http_fetch,
    verify_license,
)
from evowiki.pipeline.workspace import Workspace


def _now() -> datetime:
    return datetime.now(UTC)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m evowiki.pipeline",
                                description="EvoWiki content production pipeline (spec 8)")
    p.add_argument("--root", default="content", help="workspace directory")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    src = sub.add_parser("sources").add_subparsers(dest="action", required=True)
    src.add_parser("verify")
    ing = sub.add_parser("ingest")
    ing.add_argument("--source")
    sk = sub.add_parser("skeleton").add_subparsers(dest="action", required=True)
    exp = sk.add_parser("expand")
    exp.add_argument("--domain", required=True, choices=sorted(DOMAINS))
    exp.add_argument("--mode", choices=["replay", "record"], default="replay")
    run = sub.add_parser("run")
    which = run.add_mutually_exclusive_group(required=True)
    which.add_argument("--spine", action="store_true")
    which.add_argument("--all", action="store_true")
    which.add_argument("--concept", action="append")
    run.add_argument("--mode", choices=["replay", "record"], default="replay")
    rv = sub.add_parser("review").add_subparsers(dest="action", required=True)
    ls = rv.add_parser("list")
    ls.add_argument("--target", choices=[t.value for t in ReviewTarget])
    ls.add_argument("--state", choices=[s.value for s in ReviewState])
    rp = rv.add_parser("revise-presumes",
                       help="propose a human-reviewed move between presumes_required and "
                            "presumes_helpful (Plan 4b-1d); approve it with `review approve`")
    rp.add_argument("--segment", required=True, help="perspective segment id")
    rp.add_argument("--required", default="", help="comma-separated concept ids")
    rp.add_argument("--helpful", default="", help="comma-separated concept ids")
    rp.add_argument("--reason", required=True)
    cp = rv.add_parser("correct-presumes",
                       help="propose an additive presumption-union correction (concepts the "
                            "perspective's text needs but does not presume at all); approve it "
                            "with `review approve`")
    cp.add_argument("--segment", required=True)
    cp.add_argument("--add-required", default="")
    cp.add_argument("--add-helpful", default="")
    cp.add_argument("--reason", required=True)
    for name in ("approve", "reject"):
        d = rv.add_parser(name)
        d.add_argument("item_id")
        d.add_argument("--by", required=True)
        d.add_argument("--note", default="")
    sub.add_parser("snapshot")
    return p


def _verify(reg: SourceRegistration, fetch: Fetcher,
            now: datetime) -> tuple[SourceRegistration, OSError | None]:
    """verify_license, also returning the fetch error (if any) so the CLI can report it."""
    errors: list[OSError] = []

    def checked_fetch(url: str) -> str:
        try:
            return fetch(url)
        except OSError as exc:
            errors.append(exc)
            raise

    return verify_license(reg, checked_fetch, now), (errors[-1] if errors else None)


def _gateway(ws: Workspace, config: PipelineConfig, mode: str, env: Mapping[str, str],
             providers: Mapping[str, Provider] | None, clock: Callable[[], datetime],
             out: TextIO) -> Gateway | None:
    if mode == "record" and providers is None:
        missing = missing_env_vars(config.gateway, env)
        if missing:
            print("record mode needs these environment variables set in your own shell "
                  f"(never paste keys into chat or files): {', '.join(missing)}", file=out)
            return None
        providers = providers_from_env(env)
    return Gateway(config.gateway, ws.call_log(), mode=mode, providers=providers or {},
                   clock=clock, attempts=ws.attempt_log())


def _print_reports(reports: list[StageReport], out: TextIO) -> None:
    totals: dict[str, Counter[str]] = {}
    for r in reports:
        c = totals.setdefault(r.stage, Counter())
        c.update(created=len(r.created), flagged=len(r.flagged), dropped=len(r.dropped),
                 llm_calls=len(r.llm_call_ids))
    for stage, c in totals.items():
        print(f"{stage}: {dict(c)}", file=out)
    for r in reports:
        for d in r.dropped:
            print(f"  [{r.stage}] dropped {d}", file=out)


def _status(ws: Workspace, config: PipelineConfig, env: Mapping[str, str], out: TextIO) -> None:
    for var in sorted({*ENV_KEYS.values(), "EVOWIKI_DEEPSEEK_MODEL"}):
        print(f"{var}: {'set' if env.get(var) else 'unset'}", file=out)
    g = config.gateway
    print(f"generator: {g.generator.provider}:{g.generator.model}", file=out)
    print(f"verifier: {g.verifier.provider}:{g.verifier.model}", file=out)
    if g.single_provider:
        print("WARNING: single-provider mode — generator and verifier share a provider "
              "(spec §8 downgrade)", file=out)
    regs = ws.registry()
    print(f"sources: {len(eligible_sources(regs))} eligible / {len(regs)} registered", file=out)
    print(f"passages: {len(ws.load_passages())}", file=out)
    core = ws.load("core", Concept)
    queue = ws.queue()
    print(f"concepts: {len(core)} core, {len(approved_concepts(core, queue))} approved",
          file=out)
    for name, model in (("edges", Edge), ("segments", Segment), ("quiz_items", QuizItem)):
        counts = Counter(x.review_status.value for x in ws.load(name, model))
        print(f"{name}: {dict(sorted(counts.items()))}", file=out)
    open_items = Counter(i.target.value for i in queue.items(state=ReviewState.OPEN))
    print(f"open review items: {dict(sorted(open_items.items()))}", file=out)
    print(f"llm calls recorded: {len(ws.call_log())}", file=out)
    attempts = Counter(a.status for a in ws.attempt_log().attempts())
    print(f"llm attempts: {dict(sorted(attempts.items()))}", file=out)


def main(argv: list[str] | None = None, *, env: Mapping[str, str] = os.environ,
         fetch: Fetcher = http_fetch, clock: Callable[[], datetime] = _now,
         providers: Mapping[str, Provider] | None = None, out: TextIO = sys.stdout) -> int:
    try:
        return _main(argv, env=env, fetch=fetch, clock=clock, providers=providers, out=out)
    except (MissingRecordingError, UnknownConceptError, SnapshotValidationError) as exc:
        print(f"error: {exc}", file=out)
        return 1


def _revise_presumes(ws: Workspace, config: PipelineConfig, args: argparse.Namespace,
                     out: TextIO) -> int:
    bundle = build_bundle(ws, config)
    persp = {p.segment_id: p for p in bundle.perspectives}
    segs = {s.id: s for s in bundle.segments}
    if args.segment not in persp or args.segment not in segs:
        print(f"error: no perspective {args.segment}", file=out)
        return 1

    def ids(text: str) -> list[str]:
        return [x.strip() for x in text.split(",") if x.strip()]

    p, seg = persp[args.segment], segs[args.segment]
    try:
        if args.action == "correct-presumes":
            item = propose_presumes_correction(ws.queue(), p, seg,
                                               add_required=ids(args.add_required),
                                               add_helpful=ids(args.add_helpful),
                                               reason=args.reason)
        else:
            item = propose_presumes_revision(ws.queue(), p, seg, required=ids(args.required),
                                             helpful=ids(args.helpful), reason=args.reason)
    except PresumesRevisionError as exc:
        print(f"error: {exc}", file=out)
        return 1
    print(f"{item.id}  {item.target.value}  {item.target_id}  {item.state.value}", file=out)
    return 0


def _main(argv: list[str] | None, *, env: Mapping[str, str], fetch: Fetcher,
          clock: Callable[[], datetime], providers: Mapping[str, Provider] | None,
          out: TextIO) -> int:
    args = build_parser().parse_args(argv)
    ws = Workspace(Path(args.root))
    config = load_config(ws, env)

    if args.cmd == "status":
        _status(ws, config, env, out)
        return 0
    if args.cmd == "sources":
        regs, failures = [], 0
        for reg in ws.registry():
            if not reg.source.derivative_allowed:
                print(f"SKIP {reg.source.id} (derivatives not allowed; excluded)", file=out)
                regs.append(reg)
                continue
            new, fetch_error = _verify(reg, fetch, clock())
            regs.append(new)
            status, sid = new.source.license_check_status, reg.source.id
            if status == "verified":
                print(f"OK {sid} ({reg.source.license})", file=out)
            elif status == "unavailable":
                print(f"UNAVAILABLE {sid}: {fetch_error} (excluded until verified again)",
                      file=out)
                failures += 1
            else:
                print(f"MISMATCH {sid}: marker {reg.license_marker!r} not found at "
                      f"{reg.source.license_url} (excluded until verified again)", file=out)
                failures += 1
        ws.save_registry(regs)
        return 1 if failures else 0
    if args.cmd == "ingest":
        passages = {p.id: p for p in ws.load_passages()}
        eligible = {s.id for s in eligible_sources(ws.registry())}
        for reg in ws.registry():
            if args.source and reg.source.id != args.source:
                continue
            if reg.source.id not in eligible:
                print(f"SKIP {reg.source.id} (not eligible)", file=out)
                continue
            new = ingest_registration(reg, fetch)
            passages.update({p.id: p for p in new})
            print(f"OK {reg.source.id}: {len(new)} passages", file=out)
        ws.save_passages(passages.values())
        return 0
    if args.cmd in ("run", "skeleton"):
        gateway = _gateway(ws, config, args.mode, env, providers, clock, out)
        if gateway is None:
            return 2
        if args.cmd == "skeleton":
            report = expand_domain(load_context(ws, gateway, config), args.domain)
            _print_reports([report], out)
            return 0
        concept_ids = config.demo_spine if args.spine else (args.concept or None)
        _print_reports(run_pipeline(ws, gateway, config, clock(), concept_ids=concept_ids), out)
        return 0
    if args.cmd == "review":
        queue = ws.queue()
        if args.action == "list":
            items = queue.items(state=ReviewState(args.state) if args.state else None,
                                target=ReviewTarget(args.target) if args.target else None)
            for i in items:
                spot = " (spot-check)" if i.spot_check else ""
                print(f"{i.id}  {i.target.value}  {i.target_id}  {i.reason}  {i.state.value}{spot}",
                      file=out)
            return 0
        if args.action in ("revise-presumes", "correct-presumes"):
            return _revise_presumes(ws, config, args, out)
        state = ReviewState.APPROVED if args.action == "approve" else ReviewState.REJECTED
        if args.item_id not in {i.id for i in queue.items()}:
            print(f"error: no review item {args.item_id}", file=out)
            return 1
        try:
            item = queue.decide(args.item_id, state, decided_by=args.by, now=clock(),
                                note=args.note)
        except ValueError as exc:  # already decided
            print(f"error: {exc}", file=out)
            return 1
        print(f"{item.id} -> {item.state.value}", file=out)
        return 0
    if args.cmd == "snapshot":
        cv = snapshot_workspace(ws, config, clock())
        print(f"{cv.id} {cv.snapshot_ref}", file=out)
        return 0
    return 1
