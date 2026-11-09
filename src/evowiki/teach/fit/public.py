"""Loaders for public learner-log datasets and their translation into aspect events.

Each loader reads files from a path argument (never downloads anything) and returns
{student: [(ts, skill, correct)]} sorted by time (Anki: (ts, card, rating 1–4)). Timestamps are
UTC-aware; text is read as UTF-8 with undecodable bytes replaced (only id / outcome columns
are used). Rows a loader cannot read (missing skill, outcome not 0/1, rating not 1–4) are
skipped and counted in `LoadStats`; a file without the expected columns raises ValueError.

Datasets (real column layouts):
- ASSISTments 2009–2010 skill builder (`skill_builder_data_corrected_collapsed.csv`): user_id,
  order_id, skill_id, correct. The file has no timestamps: rows are ordered by order_id and
  given synthetic times ASSISTMENTS_STEP apart per student (little forgetting between them).
- Junyi Academy: the 2015 problem log (`user_id, exercise, time_done µs, correct true/false`) or
  the 2019 Kaggle log (`timestamp_TW, uuid, ucid, upid, is_correct`; skill = ucid).
- EdNet KT1: one `u<id>.csv` per student (timestamp ms, question_id, user_answer) plus
  `questions.csv` (question_id, correct_answer, tags); skill = first tag.
- Eedi NeurIPS 2020 task 3/4: `train_data/train_task_3_4.csv` (QuestionId, UserId, AnswerId,
  IsCorrect), `metadata/answer_metadata_task_3_4.csv` (AnswerId, DateAnswered) and
  `metadata/question_metadata_task_3_4.csv` (QuestionId, SubjectId list; skill = last subject).
  DateAnswered has no timezone (UK schools): it is read as UTC, which keeps the spacing right.
- Anki review logs: the FSRS `revlog.csv` (review_time ms, card_id, review_rating; one user per
  file) or the FSRS tiny dataset (`<user>.csv`: card_id, review_th, delta_t days, rating; times
  rebuilt per card from delta_t)."""

import csv
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from evowiki.belief.aspects import AspectEvent, DefineConcept, Observe
from evowiki.goals.contracts import Aspect, Observation, cell_id

Row = tuple[datetime, str, int]
Log = dict[str, list[Row]]

EPOCH = datetime(2009, 9, 1, tzinfo=UTC)          # synthetic start for logs without times
ASSISTMENTS_STEP = timedelta(minutes=2)
ASPECT = Aspect.PROCEDURE


@dataclass
class LoadStats:
    rows: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1


def _reader(path: Path, need: Sequence[str]) -> Iterable[dict[str, str]]:
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as f:
        r = csv.DictReader(f)
        missing = [c for c in need if c not in (r.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name}: missing columns {missing}")
        yield from r


def _finish(log: dict[str, list[Row]]) -> Log:
    return {s: sorted(rows, key=lambda r: r[0]) for s, rows in log.items() if rows}


def _bit(v: str, stats: LoadStats) -> int | None:
    v = v.strip().lower()
    if v in ("1", "true", "1.0"):
        return 1
    if v in ("0", "false", "0.0"):
        return 0
    stats.skip("outcome")
    return None


def load_assistments_2009(path: Path, stats: LoadStats | None = None) -> Log:
    stats = stats or LoadStats()
    ordered: dict[str, list[tuple[int, str, int]]] = defaultdict(list)
    for row in _reader(path, ("user_id", "order_id", "skill_id", "correct")):
        stats.rows += 1
        skill = row["skill_id"].strip()
        if not skill or skill.upper() == "NA":
            stats.skip("skill")
            continue
        y = _bit(row["correct"], stats)
        if y is not None:
            ordered[row["user_id"]].append((int(row["order_id"]), skill, y))
    log: dict[str, list[Row]] = {}
    for s, rows in ordered.items():
        rows.sort()
        log[s] = [(EPOCH + i * ASSISTMENTS_STEP, skill, y) for i, (_, skill, y) in enumerate(rows)]
    return _finish(log)


def load_junyi(path: Path, stats: LoadStats | None = None) -> Log:
    stats = stats or LoadStats()
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as f:
        header = next(csv.reader(f), [])
    log: dict[str, list[Row]] = defaultdict(list)
    if "time_done" in header:                                   # 2015 problem log
        for row in _reader(path, ("user_id", "exercise", "time_done", "correct")):
            stats.rows += 1
            y = _bit(row["correct"], stats)
            if y is None:
                continue
            ts = datetime.fromtimestamp(int(row["time_done"]) / 1e6, tz=UTC)
            log[row["user_id"]].append((ts, row["exercise"], y))
    elif "timestamp_TW" in header:                              # 2019 Kaggle log
        for row in _reader(path, ("timestamp_TW", "uuid", "ucid", "is_correct")):
            stats.rows += 1
            y = _bit(row["is_correct"], stats)
            if y is None:
                continue
            ts = datetime.fromisoformat(row["timestamp_TW"].replace(" UTC", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            log[row["uuid"]].append((ts, row["ucid"], y))
    else:
        raise ValueError(f"{path.name}: not a known Junyi layout")
    return _finish(log)


def load_ednet_kt1(dir: Path, max_students: int, stats: LoadStats | None = None) -> Log:
    stats = stats or LoadStats()
    qfile = next((p for p in (dir / "questions.csv", dir / "contents" / "questions.csv")
                  if p.is_file()), None)
    if qfile is None:
        raise ValueError(f"{dir}: no questions.csv")
    questions = {r["question_id"]: (r["correct_answer"], r["tags"].split(";")[0])
                 for r in _reader(qfile, ("question_id", "correct_answer", "tags"))}
    udir = dir / "KT1" if (dir / "KT1").is_dir() else dir
    files = sorted(udir.glob("u*.csv"), key=lambda p: (len(p.stem), p.stem))[:max_students]
    log: dict[str, list[Row]] = defaultdict(list)
    for uf in files:
        for row in _reader(uf, ("timestamp", "question_id", "user_answer")):
            stats.rows += 1
            q = questions.get(row["question_id"])
            if q is None or not q[1] or q[1] == "-1":
                stats.skip("skill")
                continue
            ts = datetime.fromtimestamp(int(row["timestamp"]) / 1000, tz=UTC)
            log[uf.stem].append((ts, q[1], int(row["user_answer"] == q[0])))
    return _finish(log)


def load_eedi_2020(path: Path, stats: LoadStats | None = None) -> Log:
    """`path` = the dataset's `data/` directory (task 3/4 files)."""
    stats = stats or LoadStats()
    subject: dict[str, str] = {}
    for r in _reader(path / "metadata" / "question_metadata_task_3_4.csv",
                     ("QuestionId", "SubjectId")):
        ids = re.findall(r"\d+", r["SubjectId"])
        if ids:
            subject[r["QuestionId"]] = ids[-1]
    when: dict[str, datetime] = {}
    for r in _reader(path / "metadata" / "answer_metadata_task_3_4.csv",
                     ("AnswerId", "DateAnswered")):
        if r["DateAnswered"]:
            when[r["AnswerId"]] = datetime.fromisoformat(r["DateAnswered"]).replace(tzinfo=UTC)
    log: dict[str, list[Row]] = defaultdict(list)
    for r in _reader(path / "train_data" / "train_task_3_4.csv",
                     ("QuestionId", "UserId", "AnswerId", "IsCorrect")):
        stats.rows += 1
        skill, ts = subject.get(r["QuestionId"]), when.get(r["AnswerId"])
        if skill is None:
            stats.skip("skill")
            continue
        if ts is None:
            stats.skip("time")
            continue
        y = _bit(r["IsCorrect"], stats)
        if y is not None:
            log[r["UserId"]].append((ts, skill, y))
    return _finish(log)


def _rating(v: str, stats: LoadStats) -> int | None:
    try:
        r = int(v)
    except ValueError:
        r = 0
    if 1 <= r <= 4:
        return r
    stats.skip("rating")
    return None


def _anki_file(path: Path, stats: LoadStats) -> list[Row]:
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as f:
        header = next(csv.reader(f), [])
    rows: list[Row] = []
    if "review_time" in header:
        for r in _reader(path, ("review_time", "card_id", "review_rating")):
            stats.rows += 1
            g = _rating(r["review_rating"], stats)
            if g is not None:
                rows.append((datetime.fromtimestamp(int(r["review_time"]) / 1000, tz=UTC),
                             r["card_id"], g))
    elif "delta_t" in header:
        last: dict[str, datetime] = {}
        for r in sorted(_reader(path, ("card_id", "review_th", "delta_t", "rating")),
                        key=lambda r: int(r["review_th"])):
            stats.rows += 1
            g = _rating(r["rating"], stats)
            if g is None:
                continue
            card, dt = r["card_id"], int(r["delta_t"])
            if card not in last or dt < 0:
                ts = EPOCH + timedelta(minutes=int(r["review_th"]))
            else:
                ts = last[card] + timedelta(days=dt, minutes=1)
            last[card] = ts
            rows.append((ts, card, g))
    else:
        raise ValueError(f"{path.name}: not a known Anki revlog layout")
    return rows


def load_anki_revlogs(path: Path, max_users: int, stats: LoadStats | None = None) -> Log:
    """A revlog file (one user, named by the file stem) or a directory of per-user files."""
    stats = stats or LoadStats()
    files = [path] if path.is_file() else sorted(
        path.glob("*.csv"), key=lambda p: (len(p.stem), p.stem))
    return _finish({f.stem: _anki_file(f, stats) for f in files[:max_users]})


def to_aspect_log(rows: Sequence[Row], *, concept_prefix: str,
                  kind: Literal["quiz", "recall"] = "quiz") -> list[AspectEvent]:
    """One concept per skill (ids `<prefix><n>`, first-seen order) with one aspect (procedure),
    defined at the first row's time; each row → an Observe. kind "quiz": correct 1 → "+", 0 →
    "-". kind "recall" (Anki ratings): 1 → "-", 2 → "~", 3 / 4 → "+"."""
    if not rows:
        return []
    if not re.fullmatch(r"[A-Za-z0-9_-]*", concept_prefix):
        raise ValueError(f"bad concept prefix {concept_prefix!r}")
    rows = sorted(rows, key=lambda r: r[0])
    concept: dict[str, str] = {}
    for _, skill, _ in rows:
        concept.setdefault(skill, f"{concept_prefix}{len(concept)}")
    t0 = rows[0][0]
    events: list[AspectEvent] = [
        DefineConcept(event_id=f"{c}-def", ts=t0, concept_id=c, aspects=(ASPECT,))
        for c in concept.values()]
    for i, (ts, skill, v) in enumerate(rows):
        if kind == "quiz":
            if v not in (0, 1):
                raise ValueError(f"quiz outcome must be 0/1, got {v}")
            pol = "+" if v == 1 else "-"
        else:
            if v not in (1, 2, 3, 4):
                raise ValueError(f"recall rating must be 1–4, got {v}")
            pol = {1: "-", 2: "~"}.get(v, "+")
        eid = f"{concept_prefix}o{i}"
        events.append(Observe(event_id=eid, ts=ts, observation=Observation(
            cell=cell_id(concept[skill], ASPECT), polarity=pol, strength=1.0, kind=kind,
            evidence_ref=eid)))
    return events
