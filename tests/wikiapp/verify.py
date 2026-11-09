"""Test helper: mark prerequisites of a user's graph verified (relation layers, user ruling
2026-10-14). Nothing in the product writes `verified=True` yet (an expert review will, source
"expert"); a test that needs a prerequisite to feed knowledge inference (KST surmise, K inference,
hard path order) verifies it here — one `GraphSaved` with its K sync, committed like any graph
change."""

from collections.abc import Iterable
from datetime import datetime

from evowiki.nodes.model import Relation
from evowiki.wikiapp.graph_sync import graph_events
from evowiki.wikiapp.repo import Commit, Repo


def verify_prereqs(repo: Repo, user_id: str, pairs: Iterable[tuple[str, str]], *,
                   now: datetime, new_id) -> None:
    """Every (src, dst) in `pairs` becomes a verified prerequisite of the user's graph: an
    existing prerequisite is marked verified, a missing one is added (source "expert")."""
    pairs = set(pairs)
    snap = repo.load(user_id)
    g = snap.state.graph
    have = {(r.src, r.dst) for r in g.relations if r.kind == "prerequisite"}
    rels = [r.model_copy(update={"verified": True})
            if r.kind == "prerequisite" and (r.src, r.dst) in pairs else r for r in g.relations]
    rels += [Relation(kind="prerequisite", src=a, dst=b, source="expert", verified=True)
             for a, b in sorted(pairs - have)]
    aspect, wiki = graph_events(snap.state, snap.k, g.updated(relations=rels), "user_edit",
                                now=now, new_id=new_id)
    repo.commit(snap, Commit(aspect=aspect, wiki=wiki))
