import inspect
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evowiki.content.graph import ContentGraph
from evowiki.content.io import load_graph, load_segments
from evowiki.content.segments import SegmentIndex
from tests.pg import ProcessDatabase, build_template, start_admin_url

FIXTURES = Path(__file__).parent / "fixtures"

# The default embedder (retrieval.embed) reads the environment; tests use the deterministic
# hashing embedder, never a model download.
os.environ.setdefault("EVOWIKI_EMBEDDER", "hash")


@pytest.fixture(autouse=True)
def _no_trace_store_leak():
    """`llm.run_context`'s step store is process-global (set by the app's lifespan); no test
    keeps it."""
    yield
    from evowiki.llm import run_context

    run_context.configure(None)


@pytest.fixture(autouse=True)
def _no_graph_listener_leak():
    """`repo.listen_graph` (set by `group_naming.register`) is process-global; no test keeps it."""
    yield
    from evowiki.wikiapp.repo import listen_graph

    listen_graph(None)


@pytest.fixture(autouse=True)
def _a2_off(monkeypatch):
    """A2 (`teach.fit.select.A2_ENABLED`, on in production) runs 1 + k real fits per cycle; tests
    run with it off and turn it on themselves (tests/teach/test_cycle_a2.py)."""
    from evowiki.teach.fit import select

    monkeypatch.setattr(select, "A2_ENABLED", False)


@pytest.fixture
def graph() -> ContentGraph:
    return load_graph(FIXTURES / "seed_graph.json")


@pytest.fixture
def segments(graph: ContentGraph) -> SegmentIndex:
    return load_segments(FIXTURES / "seed_segments.json", graph)


@pytest.fixture
def t0() -> datetime:
    return datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


@pytest.fixture(scope="session")
def dev():
    from tests.fixtures.devcontent import build_dev_content

    return build_dev_content()


@pytest.fixture(scope="session")
def pg_admin_url():
    url, handle = start_admin_url()
    build_template(url)
    yield url
    if handle is not None:
        handle.cleanup()


@pytest.fixture(scope="session")
def _process_db(pg_admin_url):
    db = ProcessDatabase(pg_admin_url)
    yield db
    db.close()


@pytest.fixture
def db_url(_process_db):
    """An empty migrated database: this process's one database, emptied before the test."""
    return _process_db.reset()


@pytest.fixture
def app_db_url(db_url):
    """`db_url` as the service connects to it (0082): role evowiki_app, which owns no table.
    `db_url` itself stays the migrating superuser's (setup, DDL, DBOS tests)."""
    from psycopg.conninfo import make_conninfo

    from evowiki.store.db import APP_ROLE
    return make_conninfo(db_url, user=APP_ROLE)


@pytest.fixture
def admin_pool(db_url):
    """The migrating superuser's pool: replaying migration SQL, the guards as the owner sees them."""
    from evowiki.store.db import make_pool
    p = make_pool(db_url, min_size=1, max_size=2)
    yield p
    p.close()


@pytest.fixture
def pool(app_db_url):
    from evowiki.store.db import make_pool
    p = make_pool(app_db_url, min_size=1, max_size=4)
    yield p
    p.close()


# --- seed recorder (R1.1 fresh-population selection, scripts/r1_1_select_seeds.py) ---------
# With EVOWIKI_SEED_RECORD=<path>, every seed passed to the CRN constructor (the single entry
# point of learner and world seeds) during the test run is written to <path> as a JSON list.
_RECORDED: set[int] = set()


def pytest_configure(config):
    if os.environ.get("EVOWIKI_SEED_RECORD"):
        from evowiki.simulator.crn import CRN
        init = CRN.__init__

        def recording_init(self, seed, *args, **kwargs):
            if isinstance(seed, int) and not isinstance(seed, bool):
                _RECORDED.add(seed)
            init(self, seed, *args, **kwargs)
        CRN.__init__ = recording_init


def pytest_unconfigure(config):
    path = os.environ.get("EVOWIKI_SEED_RECORD")
    if path:
        Path(path).write_text(json.dumps(sorted(_RECORDED)), encoding="utf-8")



# --- Chromium tests (`browser` marker) -----------------------------------------------------------
# The full suite runs for over half an hour; the headless-Chromium tests in tests/deep alone take
# about eight minutes and are reached roughly eight minutes in. A runner that kills pytest after
# ten minutes (an agent shell's foreground timeout) therefore kills it inside these tests, with no
# summary and no traceback: it looks like Playwright crashed the process, but the run was killed
# from outside. Run the full suite in the background (or with a longer timeout), or deselect
# these tests with `-m "not browser"`.
_BROWSER_USE = re.compile(r"sync_playwright|PlaywrightChecker\(|_with_browser\(")


def _source(obj) -> str:
    try:
        return inspect.getsource(obj)
    except (OSError, TypeError):
        return ""


def _launches(src: str, helpers: set[str]) -> bool:
    return bool(_BROWSER_USE.search(src)) or any(re.search(rf"\b{re.escape(h)}\b", src) for h in helpers)


def _browser_helpers(module) -> set[str]:
    """Module-level functions that launch Chromium, directly or through another one (fixpoint)."""
    funcs = {n: _source(f) for n, f in vars(module).items()
             if inspect.isfunction(f) and f.__module__ == module.__name__}
    hit: set[str] = set()
    while more := {n for n, src in funcs.items() if n not in hit and _launches(src, hit)}:
        hit |= more
    return hit


def pytest_collection_modifyitems(config, items):
    helpers: dict[str, set[str]] = {}
    for item in items:
        module, fn = getattr(item, "module", None), getattr(item, "function", None)
        if module is None or fn is None:
            continue
        if module.__name__ not in helpers:
            helpers[module.__name__] = _browser_helpers(module)
        h = helpers[module.__name__]
        defs = item._fixtureinfo.name2fixturedefs
        if _launches(_source(fn), h) or any(_launches(_source(d.func), h)
                                            for name in item.fixturenames for d in defs.get(name, ())):
            item.add_marker("browser")


def pytest_collection_finish(session):
    """Said before the first test (also under -q), so a run killed later still explains itself."""
    n = sum(1 for item in session.items if item.get_closest_marker("browser"))
    tr = session.config.pluginmanager.get_plugin("terminalreporter")
    if n and tr is not None and len(session.items) > 2000:
        tr.write_line(f"evowiki: {n} Chromium tests (~8 min) start about 8 min in; the full run takes "
                      "over 30 min. A runner that kills pytest at 10 min stops it silently inside "
                      "them. Run the full suite in the background, or deselect with -m \"not browser\".")


@pytest.fixture(autouse=True)
def _no_question(monkeypatch):
    """`goal_script.push_no_question`: the next diagnosis round on the latest Script ends at once
    (a done round with the decisions as they stand), so a test reaches the planned goal without
    answering. Any other round is the real KST pick."""
    from evowiki.wikiapp import goal
    from tests.wikiapp import goal_script
    goal_script._ASKED.clear()
    real = goal._probe

    def probe(w, draft, m, rounds):
        r = real(w, draft, m, rounds)
        from tests.wikiapp.script import Script
        sc = Script.latest
        queue = sc._queues.get("goals.probe") if sc is not None else None
        marks = [i for i, x in enumerate(queue or ()) if json.loads(x) == {"no_question": True}]
        if r.done or not marks:
            return r
        del queue[marks[0]]
        return r.model_copy(update={"pick": None, "done": True, "why": "（测试：不问）"})
    monkeypatch.setattr(goal, "_probe", probe)


@pytest.fixture
def every_skill(monkeypatch):
    """Every Skill and reference preloaded through the real path (`assistant.skills.preload`, as a
    code signal would): for tests of what the action tools do inside a turn, where the scripted
    model calls a tool without loading it first. The Skill path itself (load, gate, confirm) is
    tests/assistant/test_s3_scenarios.py's."""
    from evowiki.assistant import turn_skills
    from evowiki.assistant.skills import Preload, library
    lib = library()
    monkeypatch.setattr(turn_skills, "preload",
                        lambda **_kw: Preload(skills=lib.keys(), refs=lib.paths()))
