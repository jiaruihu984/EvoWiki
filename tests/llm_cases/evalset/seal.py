"""`run.py seal`: move the sealed share of the categories that have enough cases out of the repo to
the configured sealed directory (proposal §4: categories 1, 2, 3, 5, and only at SEALED_MIN or more).

Which cases go is `spec.is_sealed_unit` (a hash of the case's unit, subject plus failure, and a fixed seed: a
unit never crosses the line, and nobody picks). The case's text moves verbatim, with its local file (a lecture's pages and
answers). Only counts are printed: nobody, the one who runs this included, reads what moved. Running it
again moves nothing more. The moved cases stay in git history of the commits before this one: that is
what a repo is; the sealed set is sealed from here on, and new sealed cases are added by moving, never by
writing into the repo first."""

import re
import shutil

import yaml

from tests.llm_cases.evalset import spec

SEALABLE = ("claims", "grounding", "integrity", "tutoring")


def _chunks(text: str) -> tuple[str, list[str]]:
    """(the file's leading comments, one text per case). Comment and blank lines right above a case travel
    with it."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if ln.startswith("- id:")]
    cut = []
    for k, i in enumerate(starts):
        j = i
        while j > (starts[k - 1] + 1 if k else 0) and (lines[j - 1].startswith("#") or not lines[j - 1].strip()):
            j -= 1
        cut.append(j)
    ends = [*cut[1:], len(lines)]
    return "".join(lines[:cut[0]]), ["".join(lines[a:b]) for a, b in zip(cut, ends, strict=True)]


def run() -> int:
    spec.SEALED.mkdir(parents=True, exist_ok=True)
    for cat in SEALABLE:
        src = spec.CASES / f"{cat}.yaml"
        text = src.read_text(encoding="utf-8")
        head, chunks = _chunks(text)
        if len(chunks) < spec.SEALED_MIN:
            print(f"{cat}：{len(chunks)} 条，不足 {spec.SEALED_MIN}，不设密封部分")
            continue
        keep, move = [], []
        for ch in chunks:
            d = yaml.safe_load(re.sub(r"^#.*\n", "", ch, flags=re.MULTILINE))[0]
            (move if spec.is_sealed_unit(spec.unit_of(d)) else keep).append((ch, d))
        if not move:
            print(f"{cat}：{len(chunks)} 条，没有要移的")
            continue
        (spec.SEALED / "cases").mkdir(parents=True, exist_ok=True)
        dst = spec.SEALED / "cases" / f"{cat}.yaml"
        old = dst.read_text(encoding="utf-8") if dst.exists() else ""
        dst.write_text(old + "".join(ch if ch.endswith("\n") else ch + "\n" for ch, _ in move), encoding="utf-8")
        for _, d in move:
            if d.get("local"):
                (spec.SEALED / "local").mkdir(parents=True, exist_ok=True)
                f = spec.LOCAL / f"{d['local']}.yaml"
                if f.exists():
                    shutil.move(str(f), str(spec.SEALED / "local" / f.name))
        src.write_text(head + "".join(ch for ch, _ in keep), encoding="utf-8")
        print(f"{cat}：共 {len(chunks)} 条，移到密封 {len(move)} 条，留 {len(keep)} 条")
    return 0
