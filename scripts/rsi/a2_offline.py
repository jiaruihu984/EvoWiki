"""Offline validation on public learner logs.

Usage:
  uv run python scripts/rsi/a2_offline.py --data .data/public \
      --local-out .data/public/derived/a2-offline \
      --report .data/public/derived/a2-offline/generated-report.md

Design and pass criterion: the product specification (written
before the run). Per dataset of `fit_priors.DATASETS` found under --data: students with
>= --min-outcomes outcomes, a seeded sample of at most --max-students (random.Random
"<dataset>/<seed>"), each truncated to its first --max-outcomes outcomes, split by time
60 / 20 / 20 (fit / select / test):
- defaults: θ = InferParams(), no fit;
- (a) A1 alone: `fit_infer` with DEFAULT_FIT_MODEL on fit+select from the default θ;
- (b) A2: `select_model` on the events before the test window (cutoff = start of the select
  window, start = default θ, as the cycle calls it); a picked model is fitted on fit+select from
  (a)'s θ; no pick → (b) = (a);
- BKT (quiz datasets): `fit_bkt` per skill on the sample's fit+select rows (skills with
  < BKT_MIN_SEQS sequences share one pooled fit), `bkt_predict` on the test rows.
All scored on the test window. Synthetic null / power (--synthetic, on the first committable
quiz dataset's skeletons): outcomes redrawn from the product K under the default θ ("null") or
with evidence_weight.quiz = PLANTED_QUIZ_WEIGHT ("planted"), then `select_model` with
SYN_FIT fit and 60 / 120 held-out outcomes.

Only datasets with `commit=True` get numbers in --report; the others are named with a
qualitative pass / fail only. Every number goes to --local-out/a2_offline_results.json.
Reads data only through the `teach.fit.public` loaders (via fit_priors.DATASETS); runs nothing
from the data directories. No network, no paid API."""

import argparse
import importlib.util
import json
import math
import random
import statistics
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from evowiki.belief.aspects import AspectK, Observe
from evowiki.teach import infer_paths as ip
from evowiki.teach.contracts import InferParams
from evowiki.teach.fit import public
from evowiki.teach.fit.bkt import BktParams, bkt_predict, fit_bkt
from evowiki.teach.fit.fitter import fit_infer
from evowiki.teach.fit.priors import load_priors
from evowiki.teach.fit.select import A2_MIN_OUTCOMES, select_model
from evowiki.teach.layer2.method_registry import DEFAULT_FIT_MODEL
from evowiki.teach.metrics import log_loss, paired_bootstrap, seed_of
from evowiki.teach.predict import p_correct, predict_outcomes
from evowiki.wikiapp.llm import ASPECT_CONFIG_V1

_FP = Path(__file__).resolve().parent / "fit_priors.py"
_spec = importlib.util.spec_from_file_location("_fit_priors_for_a2", _FP)
fit_priors = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fit_priors)
DATASETS = fit_priors.DATASETS

BASE = ASPECT_CONFIG_V1
D = InferParams()
FIT_SHARE, SELECT_END = 0.6, 0.8
MAX_LOSS_INCREASE = 0.002       # criterion 1: CI upper bound of mean Δ below this
MIN_CHANGED = 10                # criterion 2 needs this many changed students
N_BOOT = 2000
BKT_MIN_SEQS = 20
SIZE_SPLIT = 300                # by-size table: 120–299 / ≥ 300 outcomes
PLANTED_QUIZ_WEIGHT = 0.4
SYN_FIT = 100
SYN_HELD = (60, 120)
SYN_MIN_ROWS = SYN_FIT + max(SYN_HELD)
SYN_STUDENTS = 200
NULL_FALSE_PICK_CONCERN = 0.10


# ---- per student (top level: runs in worker processes) ----

def _pairs(events, params, since) -> list[tuple[float, float]]:
    return [(o.p, o.y) for o in predict_outcomes(events, BASE, params, {}) if o.ts >= since]


def _mean_ll(pairs: Sequence[tuple[float, float]]) -> float:
    return statistics.fmean(log_loss(p, y) for p, y in pairs)


def split_cutoffs(outcome_ts: Sequence[datetime]) -> tuple[datetime, datetime]:
    """(cutoff₁, cutoff₂) = times of outcomes ⌊0.6 n⌋ and ⌊0.8 n⌋ (fit < c1 ≤ select < c2 ≤ test)."""
    n = len(outcome_ts)
    return outcome_ts[int(FIT_SHARE * n)], outcome_ts[int(SELECT_END * n)]


def run_student(job: tuple[str, list, str]) -> dict:
    sid, rows, kind = job
    t0 = time.monotonic()
    try:
        priors = load_priors()
        events = public.to_aspect_log(rows, concept_prefix="s", kind=kind)
        outs = predict_outcomes(events, BASE, D, {})
        c1, c2 = split_cutoffs([o.ts for o in outs])
        n_fit = sum(o.ts < c1 for o in outs)
        n_sel = sum(c1 <= o.ts < c2 for o in outs)
        p0 = [(o.p, o.y) for o in outs if o.ts >= c2]
        fa = fit_infer(events, BASE, D, {}, model=DEFAULT_FIT_MODEL, priors=priors, cutoff=c2)
        pa = _pairs(events, fa.params, c2)
        head = [e for e in events if e.ts < c2]          # selection never sees the test window
        pick = select_model(head, BASE, D, {}, DEFAULT_FIT_MODEL, priors, cutoff=c1,
                            seed=f"{sid}/a2")
        if pick is None:
            pb, label, cmp = pa, None, None
        else:
            label, model, cmp = pick
            fb = fit_infer(events, BASE, fa.params, {}, model=model, priors=priors, cutoff=c2)
            pb = _pairs(events, fb.params, c2)
    except Exception as e:  # noqa: BLE001 - counted and reported, never silently dropped
        return {"student": sid, "error": f"{type(e).__name__}: {str(e)[:200]}",
                "seconds": round(time.monotonic() - t0, 2)}
    ll0, lla, llb = _mean_ll(p0), _mean_ll(pa), _mean_ll(pb)
    return {"student": sid, "n": len(outs), "n_fit": n_fit, "n_select": n_sel, "n_test": len(p0),
            "cutoff2": c2.isoformat(), "ll_default": ll0, "ll_a": lla, "ll_b": llb,
            "delta": llb - lla, "changed": label is not None, "label": label,
            "select_cmp": None if cmp is None else {k: cmp[k] for k in ("mean", "ci_low",
                                                                        "ci_high", "n")},
            "pairs_default": p0, "pairs_a": pa, "pairs_b": pb,
            "seconds": round(time.monotonic() - t0, 2)}


def redraw(events: Sequence, truth: InferParams, rng: random.Random) -> list:
    """The same events with every test outcome redrawn from the product K under `truth`
    (each draw sees the earlier drawn outcomes)."""
    k = AspectK(BASE.with_infer(truth))
    out = []
    for ev in events:
        if isinstance(ev, Observe):
            o = ev.observation
            p = p_correct(k.belief(o.cell, ev.ts), o.kind, truth)
            pol = "+" if rng.random() < p else "-"
            ev = ev.model_copy(update={"observation": o.model_copy(update={"polarity": pol})})
        k.apply(ev)
        out.append(ev)
    return out


def run_synthetic(job: tuple[str, list, str, str, int]) -> dict:
    """One skeleton under one truth: `select_model` with SYN_FIT fit / `held` held-out outcomes,
    and the truth's mean held-out log-loss gain over the fitted current model."""
    sid, rows, kind, cond, held = job
    t0 = time.monotonic()
    truth = D if cond == "null" else ip.set_(D, "evidence_weight.quiz", PLANTED_QUIZ_WEIGHT)
    try:
        priors = load_priors()
        base_events = public.to_aspect_log(rows[:SYN_FIT + held], concept_prefix="s", kind=kind)
        events = redraw(base_events, truth, random.Random(f"{cond}/{sid}"))
        outs = predict_outcomes(events, BASE, D, {})
        cutoff = outs[SYN_FIT].ts
        pick = select_model(events, BASE, D, {}, DEFAULT_FIT_MODEL, priors, cutoff=cutoff,
                            seed=f"{sid}/syn/{cond}/{held}")
        fa = fit_infer(events, BASE, D, {}, model=DEFAULT_FIT_MODEL, priors=priors, cutoff=cutoff)
        gain = _mean_ll(_pairs(events, fa.params, cutoff)) - _mean_ll(_pairs(events, truth, cutoff))
    except Exception as e:  # noqa: BLE001 - counted and reported
        return {"student": sid, "cond": cond, "held": held,
                "error": f"{type(e).__name__}: {str(e)[:200]}"}
    return {"student": sid, "cond": cond, "held": held, "n_held": sum(o.ts >= cutoff for o in outs),
            "label": None if pick is None else pick[0], "truth_gain": gain,
            "seconds": round(time.monotonic() - t0, 2)}


# ---- summaries ----

def auc(pairs: Sequence[tuple[float, float]]) -> float | None:
    """Rank AUC (ties 1/2) of (score, label) pairs; labels ≥ 0.5 count as positive."""
    s = np.array([p for p, _ in pairs], dtype=float)
    y = np.array([v >= 0.5 for _, v in pairs])
    n_pos, n_neg = int(y.sum()), int((~y).sum())
    if not n_pos or not n_neg:
        return None
    order = np.argsort(s, kind="stable")
    ranks = np.empty(len(s))
    sorted_s = s[order]
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and sorted_s[j + 1] == sorted_s[i]:
            j += 1
        ranks[order[i:j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return float((ranks[y].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg))


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact binomial interval for k of n."""
    if n == 0:
        return (0.0, 1.0)

    def cdf(x: int, p: float) -> float:          # P(X ≤ x)
        if p <= 0.0:
            return 1.0
        if p >= 1.0:
            return 0.0 if x < n else 1.0
        return sum(math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                            + i * math.log(p) + (n - i) * math.log1p(-p)) for i in range(x + 1))

    def solve(f, target: float) -> float:          # f decreasing in p
        lo, hi = 0.0, 1.0
        for _ in range(60):
            mid = (lo + hi) / 2.0
            if f(mid) > target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2.0

    lower = 0.0 if k == 0 else solve(lambda p: cdf(k - 1, p), 1.0 - alpha / 2.0)
    upper = 1.0 if k == n else solve(lambda p: cdf(k, p), alpha / 2.0)
    return lower, upper


def _ci(values: Sequence[float], seed: str) -> dict | None:
    if not values:
        return None
    m, lo, hi = paired_bootstrap(values, seed=seed_of(seed), n=N_BOOT)
    return {"mean": m, "lo": lo, "hi": hi, "n": len(values)}


def summarise(rows: Sequence[dict], name: str, seed: int) -> dict:
    ok = [r for r in rows if "error" not in r]
    changed = [r for r in ok if r["changed"]]
    helped = [r for r in changed if r["delta"] < 0.0]
    out = {"students": len(rows), "errors": len(rows) - len(ok),
           "error_examples": [r["error"] for r in rows if "error" in r][:5],
           "outcomes": sum(r["n"] for r in ok), "test_outcomes": sum(r["n_test"] for r in ok),
           "delta": _ci([r["delta"] for r in ok], f"a2-offline/{name}/{seed}/delta"),
           "a_minus_default": _ci([r["ll_a"] - r["ll_default"] for r in ok],
                                  f"a2-offline/{name}/{seed}/a"),
           "ll": {k: statistics.fmean(r[f"ll_{k}"] for r in ok) if ok else None
                  for k in ("default", "a", "b")},
           "auc": {k: auc([p for r in ok for p in r[f"pairs_{k}"]]) if ok else None
                   for k in ("default", "a", "b")},
           "changed": len(changed), "helped": len(helped),
           "changed_share": len(changed) / len(ok) if ok else None,
           "helped_share_of_changed": len(helped) / len(changed) if changed else None,
           "false_selection_share_of_all": (len(changed) - len(helped)) / len(ok) if ok else None,
           "delta_changed": _ci([r["delta"] for r in changed], f"a2-offline/{name}/{seed}/dch"),
           "labels": dict(Counter(r["label"] for r in changed).most_common()),
           "helped_by_label": dict(Counter(r["label"] for r in helped).most_common()),
           "select_window": _quart([r["n_select"] for r in ok]),
           "by_size": {}}
    for tag, sub in (("120-299", [r for r in ok if r["n"] < SIZE_SPLIT]),
                     (f">={SIZE_SPLIT}", [r for r in ok if r["n"] >= SIZE_SPLIT])):
        ch = [r for r in sub if r["changed"]]
        out["by_size"][tag] = {
            "students": len(sub), "changed": len(ch),
            "helped": sum(r["delta"] < 0 for r in ch),
            "mean_delta": statistics.fmean(r["delta"] for r in sub) if sub else None}
    out.update(criteria(out))
    return out


def _quart(v: list[int]) -> dict | None:
    if not v:
        return None
    s = sorted(v)
    return {"min": s[0], "median": statistics.median(s), "max": s[-1]}


def criteria(s: dict) -> dict:
    """Criterion 1 (not worse) and 2 (helps where it acts; None = not shown, < MIN_CHANGED)."""
    c1 = s["delta"] is not None and s["delta"]["hi"] < MAX_LOSS_INCREASE
    c2 = None if s["changed"] < MIN_CHANGED else s["helped"] / s["changed"] > 0.5
    return {"criterion_1": c1, "criterion_2": c2}


def contradicts(s: dict) -> bool:
    """Eedi contradicts: CI upper bound ≥ MAX_LOSS_INCREASE, or ≥ MIN_CHANGED changed and Δ < 0
    for at most half of them."""
    return (not s["criterion_1"]) or s["criterion_2"] is False


def decide(committed: dict | None, check: dict | None) -> tuple[bool, str]:
    """(A2_ENABLED, why) from the committable dataset's summary and the local check's."""
    if committed is None:
        return False, "the committable dataset (ASSISTments) was not run"
    if not committed["criterion_1"]:
        return False, "criterion 1 failed on ASSISTments (A2 may make predictions worse)"
    if committed["criterion_2"] is None:
        return False, (f"criterion 2 not shown on ASSISTments: only {committed['changed']} "
                       f"students changed (< {MIN_CHANGED})")
    if not committed["criterion_2"]:
        return False, "criterion 2 failed on ASSISTments (A2's changes helped at most half)"
    if check is None:
        return False, "the local Eedi check was not run"
    if contradicts(check):
        return False, "the local Eedi check contradicts the ASSISTments result"
    return True, "criteria 1 and 2 pass on ASSISTments and the local Eedi check agrees"


def summarise_synthetic(rows: Sequence[dict]) -> dict:
    out: dict = {}
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["cond"], r["held"])].append(r)
    for (cond, held), rs in sorted(groups.items()):
        ok = [r for r in rs if "error" not in r]
        picked = [r for r in ok if r["label"] is not None]
        right = [r for r in picked if r["label"] == "add evidence_weight.quiz"]
        k = len(right) if cond == "planted" else len(picked)
        out[f"{cond}@{held}"] = {
            "students": len(rs), "errors": len(rs) - len(ok),
            "error_examples": [r["error"] for r in rs if "error" in r][:3],
            "picked_any": len(picked), "picked_right": len(right) if cond == "planted" else None,
            "rate": k / len(ok) if ok else None,
            "rate_ci": clopper_pearson(k, len(ok)) if ok else None,
            "labels": dict(Counter(r["label"] for r in picked).most_common()),
            "truth_gain_median": statistics.median(r["truth_gain"] for r in ok) if ok else None,
            "truth_gain_mean": statistics.fmean(r["truth_gain"] for r in ok) if ok else None}
    return out


# ---- BKT baseline ----

def bkt_baseline(sub: dict[str, list], results: Sequence[dict], seed: int) -> dict:
    """Per-skill BKT on the sample's fit+select rows, scored on each student's test rows."""
    c2 = {r["student"]: datetime.fromisoformat(r["cutoff2"]) for r in results if "error" not in r}
    train: dict[str, list[list[int]]] = defaultdict(list)
    for s, cut in c2.items():
        seqs: dict[str, list[int]] = defaultdict(list)
        for ts, skill, y in sub[s]:
            if ts < cut:
                seqs[skill].append(y)
        for skill, q in seqs.items():
            train[skill].append(q)
    if not train:
        return {"n_skills": 0}
    pooled, _ = fit_bkt([q for qs in train.values() for q in qs], iters=50, seed=seed, restarts=3)
    own: dict[str, BktParams] = {
        skill: fit_bkt(qs, iters=50, seed=seed, restarts=3)[0]
        for skill, qs in sorted(train.items()) if len(qs) >= BKT_MIN_SEQS}
    per_student: list[float] = []
    pairs: list[tuple[float, float]] = []
    for s, cut in c2.items():
        seqs: dict[str, list[tuple[datetime, int]]] = defaultdict(list)
        for ts, skill, y in sub[s]:
            seqs[skill].append((ts, y))
        mine = []
        for skill, q in seqs.items():
            ps = bkt_predict(own.get(skill, pooled), [y for _, y in q])
            mine += [(p, float(y)) for p, (ts, y) in zip(ps, q, strict=True) if ts >= cut]
        if mine:
            per_student.append(_mean_ll(mine))
            pairs += mine
    return {"n_skills_own": len(own), "n_skills": len(train),
            "ll": statistics.fmean(per_student) if per_student else None, "auc": auc(pairs)}


# ---- driver ----

def _map(fn, jobs: list, pool: ProcessPoolExecutor | None, label: str) -> list:
    out = []
    t0 = time.monotonic()
    it: Iterable = pool.map(fn, jobs, chunksize=1) if pool else map(fn, jobs)
    for i, r in enumerate(it, 1):
        out.append(r)
        if i % 25 == 0 or i == len(jobs):
            print(f"  {label}: {i}/{len(jobs)} ({time.monotonic() - t0:.0f} s)", flush=True)
    return out


def sample(log: public.Log, name: str, seed: int, min_n: int, cap: int) -> list[str]:
    eligible = sorted(s for s, rows in log.items() if len(rows) >= min_n)
    rng = random.Random(f"{name}/{seed}")
    return sorted(rng.sample(eligible, min(cap, len(eligible))))


def run_dataset(ds, root: Path, args, pool) -> dict | None:
    path = root / ds.rel
    if not path.exists():
        return None
    t0 = time.monotonic()
    stats = public.LoadStats()
    log = ds.load(path, args.max_students, stats)
    eligible = sum(len(r) >= args.min_outcomes for r in log.values())
    ids = sample(log, ds.name, args.seed, args.min_outcomes, args.max_students)
    sub = {s: log[s][:args.max_outcomes] for s in ids}
    print(f"{ds.name}: {len(ids)} of {eligible} eligible students", flush=True)
    results = _map(run_student, [(s, sub[s], ds.kind) for s in ids], pool, ds.name)
    res = {"dataset": ds.name, "title": ds.title, "license": ds.license, "commit": ds.commit,
           "kind": ds.kind, "students_total": len(log), "students_eligible": eligible,
           "summary": summarise(results, ds.name, args.seed), "students": results}
    res["bkt"] = bkt_baseline(sub, results, args.seed) if ds.kind == "quiz" else None
    if args.synthetic and ds.commit and ds.kind == "quiz":
        syn_ids = sample({s: r for s, r in log.items()}, f"{ds.name}/synthetic", args.seed,
                         SYN_MIN_ROWS, args.synthetic_students)
        jobs = [(s, log[s], ds.kind, cond, held) for cond, held in
                (("null", SYN_HELD[0]), ("planted", SYN_HELD[0]), ("planted", SYN_HELD[1]))
                for s in syn_ids]
        syn = _map(run_synthetic, jobs, pool, f"{ds.name} synthetic")
        res["synthetic"] = {"summary": summarise_synthetic(syn), "students": syn}
    res["seconds"] = round(time.monotonic() - t0, 1)
    print(f"{ds.name}: done in {res['seconds']} s", flush=True)
    return res


def _f(x, nd: int = 4) -> str:
    return "–" if x is None else f"{x:.{nd}f}"


def _ci_text(c: dict | None) -> str:
    return "–" if c is None else f"{c['mean']:+.4f} [{c['lo']:+.4f}, {c['hi']:+.4f}] (n = {c['n']})"


def _row(name: str, value: str) -> str:
    return f"| {name} | {value} |"


def report(results: list[dict], enabled: bool, why: str, args) -> str:
    head = (f"Generated by `scripts/rsi/a2_offline.py` on {datetime.now(UTC):%Y-%m-%d}; seed "
            f"{args.seed}, ≥ {args.min_outcomes} outcomes, ≤ {args.max_students} students, "
            f"first {args.max_outcomes} outcomes per student.")
    lines = [head, ""]
    word = {True: "pass", False: "fail", None: "not shown"}
    for r in results:
        s = r["summary"]
        lines += [f"### {r['title']} (`{r['dataset']}`)", f"License: {r['license']}.", ""]
        if not r["commit"]:
            agree = "contradicts" if contradicts(s) else "does not contradict"
            lines += [(f"Checked locally only (numbers not committed): criterion 1 "
                       f"{word[s['criterion_1']]}, criterion 2 {word[s['criterion_2']]}; "
                       f"it {agree} the committed result."), ""]
            continue
        bkt = r["bkt"] or {}
        lines += [
            (f"Students: {r['students_total']} in the file, {r['students_eligible']} eligible, "
             f"{s['students']} run ({s['errors']} errors), {s['outcomes']} outcomes "
             f"({s['test_outcomes']} on test); select window {s['select_window']}; "
             f"{r['seconds']} s."), "",
            "| measure | value |", "|---|---|",
            _row("mean Δ = loss(A2) − loss(A1) [95 % CI]", _ci_text(s["delta"])),
            _row("mean loss(A1) − loss(defaults) [95 % CI]", _ci_text(s["a_minus_default"])),
            _row("mean test log-loss defaults / A1 / A2",
                 f"{_f(s['ll']['default'])} / {_f(s['ll']['a'])} / {_f(s['ll']['b'])}"),
            _row("pooled test AUC defaults / A1 / A2",
                 f"{_f(s['auc']['default'])} / {_f(s['auc']['a'])} / {_f(s['auc']['b'])}"),
            _row("BKT per skill: mean test log-loss / AUC",
                 f"{_f(bkt.get('ll'))} / {_f(bkt.get('auc'))}"),
            _row("students where A2 changed the model",
                 f"{s['changed']} ({_f(s['changed_share'], 3)})"),
            _row("… of them helped on test (Δ < 0)",
                 f"{s['helped']} ({_f(s['helped_share_of_changed'], 3)})"),
            _row("mean Δ over the changed students [95 % CI]", _ci_text(s["delta_changed"])),
            _row("false selection (changed, did not help) / all",
                 _f(s["false_selection_share_of_all"], 3)),
            _row("moves picked", str(s["labels"])),
            _row("moves that helped", str(s["helped_by_label"])),
            _row("by size", str(s["by_size"])),
            _row("criterion 1 / 2", f"{s['criterion_1']} / {s['criterion_2']}"), ""]
        syn = (r.get("synthetic") or {}).get("summary")
        if syn:
            lines += ["Synthetic (real skeletons, outcomes redrawn from the product K):", "",
                      ("| condition | students | picked any | rate [95 % CI] | moves | "
                       "truth gain median / mean |"), "|---|---|---|---|---|---|"]
            for key, v in syn.items():
                ci = v["rate_ci"]
                lines.append(
                    f"| {key} | {v['students']} ({v['errors']} errors) | {v['picked_any']} | "
                    f"{_f(v['rate'], 3)} [{_f(ci[0], 3)}, {_f(ci[1], 3)}] | {v['labels']} | "
                    f"{_f(v['truth_gain_median'])} / {_f(v['truth_gain_mean'])} |")
            lines.append("")
    lines += [f"**Decision:** A2_ENABLED = {enabled} — {why}.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--local-out", type=Path, required=True)
    ap.add_argument("--report", type=Path, default=None)
    ap.add_argument("--datasets", default="assistments2009,eedi2020,fsrs_revlog,fsrs_tiny")
    ap.add_argument("--max-students", type=int, default=500)
    ap.add_argument("--min-outcomes", type=int, default=120)
    ap.add_argument("--max-outcomes", type=int, default=500)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--synthetic", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--synthetic-students", type=int, default=SYN_STUDENTS)
    ap.add_argument("--workers", type=int, default=24)
    args = ap.parse_args(argv)
    wanted = args.datasets.split(",")
    unknown = set(wanted) - {d.name for d in DATASETS}
    if unknown:
        raise SystemExit(f"unknown datasets {sorted(unknown)}")
    t0 = time.monotonic()
    results = []
    pool = ProcessPoolExecutor(max_workers=args.workers) if args.workers > 1 else None
    try:
        for ds in DATASETS:
            if ds.name in wanted:
                r = run_dataset(ds, args.data, args, pool)
                if r is None:
                    print(f"{ds.name}: not found under {args.data}, skipped", flush=True)
                else:
                    results.append(r)
    finally:
        if pool:
            pool.shutdown()
    by = {r["dataset"]: r["summary"] for r in results}
    committed = next((r["summary"] for r in results if r["commit"] and r["kind"] == "quiz"), None)
    enabled, why = decide(committed, by.get("eedi2020"))
    args.local_out.mkdir(parents=True, exist_ok=True)
    out = {"seed": args.seed, "args": {k: str(v) for k, v in vars(args).items()},
           "a2_min_outcomes": A2_MIN_OUTCOMES, "planted_quiz_weight": PLANTED_QUIZ_WEIGHT,
           "a2_enabled": enabled, "why": why, "seconds": round(time.monotonic() - t0, 1),
           "results": results}
    (args.local_out / "a2_offline_results.json").write_text(json.dumps(out, indent=1),
                                                            encoding="utf-8")
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(report(results, enabled, why, args), encoding="utf-8")
    print(f"A2_ENABLED = {enabled}: {why} ({out['seconds']} s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
