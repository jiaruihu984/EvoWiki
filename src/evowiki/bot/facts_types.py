"""DB-free fact types the Bot rules and gate read (kept apart from `facts.py`, which loads them
from the database), so the pure modules never import store/psycopg code."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class WrongAnswer:
    event_id: str            # the aspect event id (evidence)
    cell: str                # "c1.interpret"
    question: str            # stable question key (see question_key)
    at: datetime
    misconception: str | None

    @property
    def page_id(self) -> str | None:
        parts = self.question.split(":")
        if len(parts) >= 3 and parts[0] in {"check", "review", "block"} and parts[1] != "-":
            return parts[1]
        return None


@dataclass(frozen=True)
class DueReview:
    count: int
    cells: tuple[str, ...]
    first_page_id: str | None


@dataclass(frozen=True)
class GoalFact:
    path_id: str
    title: str
    next_page_id: str | None     # first page not done (None: not started yet)
    last_opened: datetime | None  # max opened_at over the path's pages
    created_at: datetime


@dataclass(frozen=True)
class OpenQuestion:
    session_id: str
    page_id: str
    text: str                    # the 还没懂的 field
    at: datetime                 # compaction time
    page_opened_since: bool      # page opened after `at`


@dataclass(frozen=True)
class SignalFact:
    event_id: str
    at: datetime
    kind: str
    polarity: int                # teach.signal_kinds.polarity
    # written page feedback (`page_feedback` with text): the Bot starts no cycle for it (its
    # signal counts toward the user's next RSI round, due by signal count)
    written: bool = False


@dataclass(frozen=True)
class SourceMatchFact:
    """A `SourceMatched` event: the material and the pages it covers."""
    event_id: str
    source_id: str
    title: str                           # the material's title (untrusted text)
    pages: tuple[tuple[str, str], ...]   # (page_id, page title), at most 3


@dataclass(frozen=True)
class HandEditFact:
    """A `HandEdited` event: the page and the sections whose text he changed."""
    event_id: str
    page_id: str
    sections: tuple[str, ...]


@dataclass(frozen=True)
class UserFacts:
    user_id: str
    wrong_answers: tuple[WrongAnswer, ...]     # within the last `wrong_days`, oldest first
    due_review: DueReview
    goals: tuple[GoalFact, ...]                # unfinished paths only
    open_questions: tuple[OpenQuestion, ...]
    # last 20 signals about the explanation (rules.TEACHING_KINDS) with polarity != 0, oldest first
    signals: tuple[SignalFact, ...]
    names: dict[str, str]                      # cell, concept and page id -> display name
    # page id -> title with its path position (`标题（2/5）`) for the pages a rule titles an intent by
    labels: dict[str, str] = field(default_factory=dict)
    # materials matched to pages within the last `SOURCE_MATCH_DAYS`, oldest first
    source_matches: tuple[SourceMatchFact, ...] = ()
    # hand edits of the last `SOURCE_MATCH_DAYS`, oldest first
    hand_edits: tuple[HandEditFact, ...] = ()


@dataclass(frozen=True)
class WeakPrereq:
    """A prerequisite (graph node) of the next page or the two after it that is weak: a measured
    K cell of it is gap / shaky (not just declared), or it was answered wrong this week."""
    node_id: str
    name: str
    for_title: str               # the page that needs it
    # how the path teaches it (goal_week._weak): "page" (only later pages teach it; `page_key` may
    # move right before the page that needs it), "missing" (no page: the Bot may insert one right
    # before it), "ordered" (an unfinished page teaching it already comes first), "done" (a
    # finished page teaches it), "fixed" (the Bot leaves it). Only "page" / "missing" act.
    taught: str
    page_key: str | None = None
    page_title: str | None = None
    before_key: str | None = None    # the page that needs it: an edit goes right before it


@dataclass(frozen=True)
class StuckPage:
    page_id: str
    title: str
    seconds: int


@dataclass(frozen=True)
class GoalWeek:
    """What the weekly goal review reads of one active (unfinished, not parked) learning path, over
    the 7 local days before the review (B5 spec §1.2). All deterministic facts, no LLM. JSON-able
    (`dataclasses.asdict`): stored as the week's snapshot."""
    path_id: str
    title: str
    pages_shown: int
    pages_done: int
    done_this_week: int
    new_pages_this_week: int         # path pages first written this week
    next_key: str | None             # the first shown, startable page not done
    next_page_id: str | None         # its wiki page (None: not started yet)
    next_title: str | None
    next_position: int | None        # 1-based in the shown order
    active_days: int                 # local days with a path page opened / an answer / a signal
    review_due: int                  # due + overdue review cells of the path's concepts
    review_cap: int                  # ReviewSettings.max_per_day
    wrong_cells: tuple[str, ...]     # names of path cells answered wrong >= wrong_count times
    weak_prereqs: tuple[WeakPrereq, ...]
    known_share: float | None        # remaining pages' cells already known (None: no cells)
    idle_declines: int               # goal_idle cards of this goal declined in the last 30 days
    days_since_open: int             # since a path page was last opened (or the path was made)
    reading_seconds: int | None      # this week on the path's pages; None: unknown (§1.6)
    stuck: StuckPage | None          # the unfinished page read longest this week (when known)
    last_week: dict | None = None    # the previous review's snapshot of this path


@dataclass(frozen=True)
class PageFacts:
    """What the page-chip rule sees of one page (read when the page's suggestions are listed)."""
    page_id: str
    label: str                       # title with its path position
    open_text: str | None            # newest 还没懂的 of the sessions bound to the page


def question_key(evidence_ref: str) -> str:
    """The stable key of the question an answer's evidence reference is about."""
    if evidence_ref.startswith(("review:", "block:")):
        head, sep, _ = evidence_ref.rpartition(":")
        return head if sep else evidence_ref
    return evidence_ref
