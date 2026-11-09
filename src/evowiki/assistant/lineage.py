"""Assistant artifacts (RSI observability plan 2026-10-29 §5 1b): registered by the session stores
when the entry is saved (its own transaction in Postgres).

  answer      → the answer's message id (an answer never changes: the id is the version); a 👎 on
                it targets this artifact (`signals.assistant_ratings`)
  compaction  → the compaction entry id; parent: the session's previous compaction (`previous`),
                whose summary the new one folds in"""

from evowiki.assistant.session import Entry
from evowiki.store.artifacts import Artifact, Dependency, Lineage, artifact_id, current_run_id


def answer_artifact(user_id: str, message_id: str) -> str:
    return artifact_id(user_id, "answer", message_id, message_id)


def compaction_artifact(user_id: str, entry_id: str) -> str:
    return artifact_id(user_id, "compaction", entry_id, entry_id)


def entry_lineage(user_id: str, entry: Entry, previous: Entry | None = None) -> Lineage:
    """What saving `entry` registers; `previous`: the session's compaction before it (compaction
    entries only). Empty for every other entry type."""
    run = current_run_id()
    if entry.type == "assistant":
        mid = str(entry.payload.get("message_id") or entry.entry_id)
        return Lineage((Artifact(user_id, "answer", mid, mid, run, entry.at),))
    if entry.type != "compaction":
        return Lineage()
    me = Artifact(user_id, "compaction", entry.entry_id, entry.entry_id, run, entry.at)
    if previous is None:
        return Lineage((me,))
    prev = Artifact(user_id, "compaction", previous.entry_id, previous.entry_id, None, previous.at)
    return Lineage((me, prev), (Dependency(me.artifact_id, prev.artifact_id, "previous"),))
