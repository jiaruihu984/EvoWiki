"""Fit hierarchical priors for the A1 fitter's θ_infer paths from public learner logs.

Usage:
  uv run python -I scripts/rsi/fit_priors.py --data .data/public \
      --out src/evowiki/teach/fit/priors.json \
      --report .data/public/derived/generated-report.md \
      --local-out .data/public/derived

The generated report holds only committable numbers; the committed report
(the product specification) is maintained by hand from it.

Per dataset found under --data (see DATASETS):
(a) students with >= --min-outcomes outcomes, at most --max-students of them (seeded sample,
    --seed), each truncated to its first --max-outcomes outcomes: `fit_infer` on the product's
    aspect K (`to_aspect_log`, one aspect per skill) with the dataset's paths (start_known,
    the K's belief before any evidence, included since fit-priors-v2) and a flat prior
    (sd_logit FLAT_SD) → the per-student MAPs (reported: mean / sd of their logits, median, IQR,
    share at a bound). The hierarchical prior itself is the type-II ML population normal per
    path (`hier.fit_population`) over each student's log-likelihood on a logit grid
    (--eb-grid points per path; --eb-grid-3 for three, --eb-grid-4 for four), sd floored at SD_FLOOR: flat
    MAPs of weakly identified paths pile up at the bounds, so their sample sd measures noise.
(b) cross-checks: `fit_bkt` per skill (skills with >= BKT_MIN_STUDENTS students among the
    sample, the BKT_MAX_SKILLS most frequent) → population medians of guess / slip; for Anki
    logs, FSRS parameters via `fsrs_rs_python` when the optional `fit` extra is installed
    (reported only, never used as product parameters).
(c) priors: only datasets with `commit=True` (license confirmed for derived numbers) write into
    --out, with source "<dataset>@<DERIVATION>" (several committable datasets: their students
    are pooled); every other path keeps its current entry, except entries an earlier derivation
    wrote, which are reset to the default first. Fail closed: a fitted path whose fit
    sits at a bound (more than MAX_AT_BOUND of the students' flat MAPs at a bound, or the
    population mean within one grid step of a bound) gets the default prior, and the report
    lists it under "Not written". Every dataset's numbers go to
    --local-out/fit_priors_results.json; --report (committed) gets only committable numbers and
    names the local-only datasets without their numbers.

Reads data files only through `evowiki.teach.fit.public` loaders; runs nothing from the data
directories. No network, no paid API."""

import argparse
import itertools
import json
import math
import random
import statistics
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import FitModel, InferParams
from evowiki.teach.fit import public
from evowiki.teach.fit.bkt import fit_bkt
from evowiki.teach.fit.fitter import fit_infer
from evowiki.teach.fit.hier import fit_population
from evowiki.teach.fit.priors import (
    EDGE,
    PRIORS_FILE,
    PathPrior,
    default_prior,
    from_logit,
    load_priors,
    to_logit,
)
from evowiki.teach.metrics import log_loss
from evowiki.teach.predict import predict_outcomes
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1

DERIVATION = "fit-priors-v2"           # v2: start_known fitted with slip / guess
FLAT_SD = 1e3
BKT_MIN_STUDENTS = 20
BKT_MAX_SKILLS = 150
SD_FLOOR = 0.5
MAX_AT_BOUND = 0.5            # a path with more of its students' flat MAPs at a bound is not written
QUIZ_PATHS = ("slip", "guess_scale", "start_known")
RECALL_PATHS = ("slip", "guess_scale", "forgetting_scale", "start_known")
_DERIVED = "@fit-priors-v"                # any derivation's source suffix


@dataclass(frozen=True)
class Dataset:
    name: str
    title: str
    rel: str                                   # path under --data
    load: Callable[[Path, int, public.LoadStats], public.Log]  # (path, max_students, stats)
    kind: str                                  # quiz | recall
    license: str
    commit: bool                               # derived numbers may be committed


DATASETS = (
    Dataset("assistments2009", "ASSISTments 2009–2010 skill builder (corrected, collapsed)",
            "assistments2009/skill_builder_data_corrected_collapsed.csv",
            lambda p, n, st: public.load_assistments_2009(p, st), "quiz",
            "ASSISTments data terms: free to use with attribution (Feng, Heffernan & Koedinger "
            "2009); the data itself may not be passed on", True),
    Dataset("eedi2020", "Eedi NeurIPS 2020 Education Challenge (task 3/4)",
            "eedi2020/unzipped/data", lambda p, n, st: public.load_eedi_2020(p, st), "quiz",
            "CC BY-NC-ND 4.0", False),
    Dataset("junyi", "Junyi Academy problem log", "junyi/log.csv",
            lambda p, n, st: public.load_junyi(p, st), "quiz", "not confirmed", False),
    Dataset("ednet_kt1", "EdNet KT1", "ednet_kt1", public.load_ednet_kt1, "quiz",
            "not confirmed", False),
    Dataset("fsrs_revlog", "FSRS sample Anki revlog (one user)", "fsrs_revlog/revlog.csv",
            public.load_anki_revlogs, "recall", "no license", False),
    Dataset("fsrs_tiny", "FSRS tiny dataset (10 Anki users)", "fsrs_tiny/unzipped",
            public.load_anki_revlogs, "recall", "no license", False),
)


def _flat(paths: tuple[str, ...]) -> dict[str, PathPrior]:
    d = InferParams()
    return {p: PathPrior(mean=ip.get(d, p), sd_logit=FLAT_SD, source="flat") for p in paths}


def eb_axis(n: int) -> list[float]:
    """Logit grid of the population fit: n points evenly in ±logit(1 - EDGE) (the fitter's)."""
    zmax = math.log((1.0 - EDGE) / EDGE)
    return [float(z) for z in np.linspace(-zmax, zmax, n)]


def fit_student(job: tuple[str, list[public.Row], str, tuple[str, ...], int]) -> dict:
    """One student (top level: runs in worker processes): the flat-prior MAP of `paths` by
    `fit_infer`, and the log-likelihood Σ log p(y) on the product grid eb_axis(eb_grid)^k."""
    sid, rows, kind, paths, eb_grid = job
    events = public.to_aspect_log(rows, concept_prefix="s", kind=kind)
    start = InferParams()
    r = fit_infer(events, ASPECT_CONFIG_V1, start, {},
                  model=FitModel(version=1, paths=paths, prior_strength=0.125),
                  priors=_flat(paths), cutoff=None)
    axis = eb_axis(eb_grid)
    ll = np.empty((eb_grid,) * len(paths))
    for idx in itertools.product(range(eb_grid), repeat=len(paths)):
        params = start
        for p, i in zip(paths, idx, strict=True):
            params = ip.set_(params, p, from_logit(p, axis[i]))
        ll[idx] = -sum(log_loss(o.p, o.y)
                       for o in predict_outcomes(events, ASPECT_CONFIG_V1, params, {}))
    return {"student": sid, "n": r.n_train,
            "map": {p: ip.get(r.params, p) for p in paths},
            "gain_per_outcome": (r.loglik_train - r.loglik_start) / r.n_train,
            "ll_grid": ll.tolist()}


def eb_grid_for(n_paths: int, args) -> int:
    return args.eb_grid if n_paths <= 2 else args.eb_grid_3 if n_paths == 3 else args.eb_grid_4


def _population(fits: list[dict], path: str) -> dict:
    vals = [f["map"][path] for f in fits]
    z = [to_logit(path, v) for v in vals]
    edge = sum(_at_bound(path, v) for v in vals) / len(vals)
    return {"n": len(vals), "mean_logit": statistics.fmean(z),
            "sd_logit": statistics.stdev(z) if len(z) > 1 else None,
            "mean": from_logit(path, statistics.fmean(z)), "median": statistics.median(vals),
            "q25": _q(vals, 0.25), "q75": _q(vals, 0.75), "at_edge": edge}


def _q(v: list[float], q: float) -> float:
    s = sorted(v)
    return s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]


def _bkt(log: public.Log, kind: str, seed: int) -> dict:
    per_skill: dict[str, list[list[int]]] = defaultdict(list)
    for rows in log.values():
        seqs: dict[str, list[int]] = defaultdict(list)
        for _, skill, v in rows:
            seqs[skill].append(v if kind == "quiz" else int(v >= 2))
        for skill, s in seqs.items():
            per_skill[skill].append(s)
    skills = sorted((s for s, q in per_skill.items() if len(q) >= BKT_MIN_STUDENTS),
                    key=lambda s: (-len(per_skill[s]), s))[:BKT_MAX_SKILLS]
    if not skills:
        return {"n_skills": 0}
    fits = [fit_bkt(per_skill[s], iters=50, seed=seed, restarts=3)[0] for s in skills]
    out: dict = {"n_skills": len(skills)}
    for name in ("prior", "learn", "forget", "guess", "slip"):
        v = [getattr(p, name) for p in fits]
        out[name] = {"median": statistics.median(v), "q25": _q(v, 0.25), "q75": _q(v, 0.75)}
    return out


def _fsrs(log: public.Log) -> dict:
    try:
        from fsrs_rs_python import DEFAULT_PARAMETERS, FSRS, FSRSItem, FSRSReview  # `fit` extra
    except ImportError:
        return {"status": "not installed (optional `fit` extra)"}
    items = []
    for rows in log.values():
        by_card: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
        for ts, card, g in rows:
            by_card[card].append((ts, g))
        for revs in by_card.values():
            hist: list = []
            prev = None
            for ts, g in revs:
                dt = 0 if prev is None else max(0, (ts.date() - prev.date()).days)
                hist.append(FSRSReview(g, dt))
                prev = ts
                if len(hist) >= 2 and dt > 0:
                    items.append(FSRSItem(list(hist)))
    if not items:
        return {"status": "no usable review histories"}
    params = FSRS(DEFAULT_PARAMETERS).compute_parameters(items)
    return {"status": "ok", "n_items": len(items), "parameters": [round(p, 4) for p in params]}


def run_dataset(ds: Dataset, root: Path, args, pool: ProcessPoolExecutor) -> dict | None:
    path = root / ds.rel
    if not path.exists():
        return None
    t0 = time.monotonic()
    stats = public.LoadStats()
    log = ds.load(path, args.max_students, stats)
    eligible = sorted(s for s, rows in log.items() if len(rows) >= args.min_outcomes)
    rng = random.Random(f"{ds.name}/{args.seed}")
    sample = sorted(rng.sample(eligible, min(args.max_students, len(eligible))))
    sub = {s: log[s][:args.max_outcomes] for s in sample}
    paths = QUIZ_PATHS if ds.kind == "quiz" else RECALL_PATHS
    eb_grid = eb_grid_for(len(paths), args)
    jobs = [(s, sub[s], ds.kind, paths, eb_grid) for s in sample]
    fits = list(pool.map(fit_student, jobs, chunksize=4)) if jobs else []
    t_fit = time.monotonic() - t0
    res = {"dataset": ds.name, "title": ds.title, "license": ds.license, "commit": ds.commit,
           "students_total": len(log), "students_eligible": len(eligible),
           "students_fitted": len(fits), "outcomes_fitted": sum(f["n"] for f in fits),
           "rows_read": stats.rows, "paths": list(paths),
           "population": {p: _population(fits, p) for p in paths} if fits else {},
           "mean_gain_per_outcome": statistics.fmean(f["gain_per_outcome"] for f in fits)
           if fits else None,
           "eb_grid": eb_grid, "eb": _eb(fits, paths, eb_grid) if len(fits) > 1 else None,
           "fit_seconds": round(t_fit, 1), "fits": fits}
    res["bkt"] = _bkt(sub, ds.kind, args.seed)
    if ds.kind == "recall":
        res["fsrs"] = _fsrs({s: log[s] for s in sample})
    res["seconds"] = round(time.monotonic() - t0, 1)
    print(f"{ds.name}: {len(fits)} students, {res['seconds']} s", flush=True)
    return res


def _eb(fits: list[dict], paths: tuple[str, ...], eb_grid: int) -> dict:
    """Type-II ML population normal per path (hier.fit_population) over the students' grids."""
    axis = eb_axis(eb_grid)
    mu, sd, total = fit_population(np.array([f["ll_grid"] for f in fits]), [axis] * len(paths),
                                   sd_floor=SD_FLOOR)
    return {"marginal_loglik": total,
            "paths": {p: {"mean_logit": m, "sd_logit": s, "mean": from_logit(p, m)}
                      for p, m, s in zip(paths, mu, sd, strict=True)}}


def _at_bound(path: str, v: float) -> bool:
    lo, hi = ip.bounds(path)
    return min(v - lo, hi - v) <= 0.01 * (hi - lo)


def committed_priors(results: list[dict], current: dict[str, PathPrior]
                     ) -> tuple[dict[str, PathPrior], dict[str, str]]:
    """Committable datasets' students pooled per path set → the population normal per path, and
    the paths refused (→ the default prior) with the reason. Fail closed: a path whose fit sits at
    a bound (more than MAX_AT_BOUND of the students' flat MAPs at a bound, or the population mean
    within one grid step of a bound) is not identified by the data and is not written."""
    # entries an earlier run derived are reset first, so a re-run reproduces the file from any state
    out = {p: default_prior(p) if _DERIVED in pr.source else pr for p, pr in current.items()}
    refused: dict[str, str] = {}
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in results:
        if r["commit"] and len(r["fits"]) > 1:
            groups[(tuple(r["paths"]), r["eb_grid"])].append(r)
    written: set[str] = set()
    for (paths, eb_grid), rs in groups.items():
        clash = written & set(paths)
        if clash:
            raise ValueError(f"paths {sorted(clash)} come from two committable path sets")
        written |= set(paths)
        fits = [f for r in rs for f in r["fits"]]
        eb = _eb(fits, paths, eb_grid)
        src = "+".join(r["dataset"] for r in rs) + f"@{DERIVATION}"
        axis = eb_axis(eb_grid)
        step = axis[1] - axis[0]
        for p, e in eb["paths"].items():
            share = sum(_at_bound(p, f["map"][p]) for f in fits) / len(fits)
            if share > MAX_AT_BOUND:
                refused[p] = f"{share:.0%} of the students' flat MAPs at a bound"
            elif abs(e["mean_logit"]) >= axis[-1] - step:
                refused[p] = "population mean within one grid step of a bound"
            if p in refused:
                out[p] = default_prior(p)
                continue
            out[p] = PathPrior(mean=round(e["mean"], 4), sd_logit=round(e["sd_logit"], 4),
                               source=src)
    return out, refused


def write_priors(file: Path, priors: dict[str, PathPrior]) -> None:
    data = {p: {"mean": pr.mean, "sd_logit": pr.sd_logit, "source": pr.source}
            for p, pr in priors.items()}
    file.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    load_priors(file)                                         # fail closed on a bad write


def _fmt(x: float | None, nd: int = 3) -> str:
    return "–" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{nd}f}"


def report(results: list[dict], priors: dict[str, PathPrior], refused: dict[str, str],
           args) -> str:
    head = (f"Generated by `scripts/rsi/fit_priors.py` ({DERIVATION}) on "
            f"{datetime.now(UTC):%Y-%m-%d}; seed {args.seed}, ≥ {args.min_outcomes} outcomes "
            f"per student, ≤ {args.max_students} students per dataset, first "
            f"{args.max_outcomes} outcomes per student.")
    lines = [head, ""]
    for r in results:
        lines.append(f"### {r['title']} (`{r['dataset']}`)")
        lines.append(f"License: {r['license']}.")
        if not r["commit"]:
            lines += ["", "Checked locally only: its derived numbers are not committed.", ""]
            continue
        who = (f"Students: {r['students_total']} in the file, {r['students_eligible']} "
               f"with ≥ {args.min_outcomes} outcomes, {r['students_fitted']} fitted "
               f"({r['outcomes_fitted']} outcomes; {r['fit_seconds']} s). Mean log-lik gain "
               f"per outcome over the defaults: {_fmt(r['mean_gain_per_outcome'], 4)}.")
        lines += ["", who, "",
                  ("| path | population mean (type-II ML) | sd (logit) | flat-MAP median | "
                   "flat-MAP IQR | flat-MAP at a bound | flat-MAP sd (logit) |"),
                  "|---|---|---|---|---|---|---|"]
        for p, s in r["population"].items():
            e = r["eb"]["paths"][p]
            lines.append(f"| {p} | {_fmt(e['mean'])} | {_fmt(e['sd_logit'])} | "
                         f"{_fmt(s['median'])} | {_fmt(s['q25'])}–{_fmt(s['q75'])} | "
                         f"{s['at_edge']:.0%} | {_fmt(s['sd_logit'])} |")
        b = r["bkt"]
        if b.get("n_skills"):
            bkt = (f"BKT cross-check ({b['n_skills']} skills, EM per skill): guess median "
                   f"{_fmt(b['guess']['median'])} (IQR {_fmt(b['guess']['q25'])}–"
                   f"{_fmt(b['guess']['q75'])}), slip median {_fmt(b['slip']['median'])} "
                   f"(IQR {_fmt(b['slip']['q25'])}–{_fmt(b['slip']['q75'])}), learn "
                   f"{_fmt(b['learn']['median'])}, forget {_fmt(b['forget']['median'])}, P(known at the "
                   f"first opportunity) {_fmt(b['prior']['median'])}.")
            lines += ["", bkt]
        lines.append("")
    if refused:
        lines += ["### Not written (fit at a bound: kept the default prior)", ""]
        lines += [f"- {p}: {why}" for p, why in refused.items()]
        lines.append("")
    lines += ["### Priors written", "", "| path | mean | sd (logit) | source |", "|---|---|---|---|"]
    for p, pr in priors.items():
        lines.append(f"| {p} | {pr.mean:g} | {pr.sd_logit:g} | {pr.source} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=PRIORS_FILE)
    ap.add_argument("--report", type=Path, default=None)
    ap.add_argument("--local-out", type=Path, required=True)
    ap.add_argument("--datasets", default=",".join(d.name for d in DATASETS))
    ap.add_argument("--max-students", type=int, default=2000)
    ap.add_argument("--min-outcomes", type=int, default=30)
    ap.add_argument("--max-outcomes", type=int, default=500)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--eb-grid", type=int, default=15, help="grid points per path (≤ 2 paths)")
    ap.add_argument("--eb-grid-3", type=int, default=11, help="grid points per path (3 paths)")
    ap.add_argument("--eb-grid-4", type=int, default=5, help="grid points per path (4 paths)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    wanted = set(args.datasets.split(","))
    unknown = wanted - {d.name for d in DATASETS}
    if unknown:
        raise SystemExit(f"unknown datasets {sorted(unknown)}")
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for ds in DATASETS:
            if ds.name in wanted:
                r = run_dataset(ds, args.data, args, pool)
                if r is None:
                    print(f"{ds.name}: not found under {args.data}, skipped")
                else:
                    results.append(r)
    args.local_out.mkdir(parents=True, exist_ok=True)
    (args.local_out / "fit_priors_results.json").write_text(
        json.dumps(results, indent=1, default=str), encoding="utf-8")
    priors, refused = committed_priors(results, load_priors(args.out))
    write_priors(args.out, priors)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report(results, priors, refused, args), encoding="utf-8")
    print(f"wrote {args.out}; local results in {args.local_out}")


if __name__ == "__main__":
    main()
