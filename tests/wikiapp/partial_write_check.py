"""Shared assertions: a partial page write (feedback / card / assistant / practice) leaves a
WriteMark of its kind on the record and a `section_logs_saved` event for the sections it changed."""

from evowiki.wikiapp.lineage import section_artifact


def assert_partial_write(repo, pid, kind, sections, *, producer, user_id="u1", artifacts=True):
    rec = repo.load(user_id).state.pages[pid]
    w = rec.written
    assert w is not None and w.kind == kind
    assert set(sections) <= set(w.sections)
    assert w.job_id
    events = [ev for _, ev in repo.wiki_events_since(user_id, {"section_logs_saved"}, after_seq=0)
              if ev.body.logs[0].page_id == pid]
    logged = {lg.section_id: lg for lg in events[-1].body.logs}      # the write's own event
    assert set(logged) == set(w.sections) and set(sections) <= set(logged)
    assert {lg.producer for lg in logged.values()} == {producer}
    if artifacts and hasattr(repo, "artifacts"):     # PgRepo keeps them in the database
        refs = {a.ref: a for a in repo.artifacts.of_kind("section")}
        for sid in sections:
            if sid in rec.page.texts:
                assert refs[f"{pid}/{sid}"].artifact_id == section_artifact(
                    user_id, rec.page, sid)
    return rec
