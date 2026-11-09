from evowiki.harness.experience import build_dossiers, comparison_batches, object_keys, read_page


def test_search_entire_corpus_without_unrelated_fallback():
    records = [{"exp_id": str(i), "situation": "ordinary", "evidence": {}}
               for i in range(300)]
    records[0]["situation"] = "orthogonality counterexample"
    dossiers = build_dossiers([], [], records, [], [])
    page = read_page(dossiers, user_id="u", query="orthogonality", limit=1)
    assert page["items"][0]["id"] == "experience:0"
    assert not read_page(dossiers, user_id="u", query="unfindableword")["items"]


def test_exact_dossier_links_joint_effect_and_observation_timeline():
    rounds = [{"round_id": "r", "finding": "clarity", "candidates": [
        {"kind": "general", "diff": {"ops": [{"name": "H1.assistant"}]}}]}]
    changes = [{"change_id": "c", "round_id": "r", "status": "rolled_back", "diff": {
        "ops": [{"name": "H1.assistant"}, {"name": "H2.assistant.search"}]}}]
    obs = [{"obs_id": "o2", "change_id": "c", "at": 2},
           {"obs_id": "o1", "change_id": "c", "at": 1}]
    exps = [{"exp_id": "e", "evidence": {"round_id": "r"}},
            {"exp_id": "orphan", "evidence": {"change_id": "missing"}, "change": "old"}]
    gates = [{"round_id": "r", "subject": "diff", "verdict": "pass"}]
    dossiers = build_dossiers(rounds, changes, exps, obs, gates)
    change = next(d for d in dossiers if d["id"] == "change:c")
    assert change["joint_change"]
    assert not change["experiences"]
    assert [o["obs_id"] for o in change["observations"]] == ["o1", "o2"]
    assert change["gates"] == gates
    assert next(d for d in dossiers if d["id"] == "change:missing")["experiences"]
    first = read_page(dossiers, user_id="u", mode="change", change_id="c", limit=1)
    assert first["has_more"]
    assert "observation:o1" not in first["sources"]
    second = read_page(dossiers, user_id="u", mode="change", change_id="c", cursor=1, limit=1)
    assert second["sources"] == ["change:c", "observation:o1"]


def test_typed_object_identity_keeps_full_keys_and_operation_scope():
    keys = object_keys({"ops": [{"name": "H2.assistant.search"}], "spec_ops": [
        {"op": "set_param", "track": "B", "path": "style.detail"},
        {"op": "edit_item", "item_id": "i"}, {"op": "add_item"}]}, "candidate:r:a")
    assert keys == ["candidate-operation:candidate:r:a:2", "item:i",
                    "pack:H2.assistant.search", "parameter:B:style.detail"]


def test_comparison_batches_cover_old_and_new_attempts_with_bounded_inputs():
    records = [{"id": str(i)} for i in range(300)]
    batches = list(comparison_batches(records))
    assert all(len(batch) <= 8 for batch in batches)
    assert {r["id"] for batch in batches for r in batch} == {str(i) for i in range(300)}
    assert all(batch[0] == records[0] for batch in batches)


def test_large_record_paging_separates_read_citations_from_full_dependencies():
    record = {'id': 'experience:e', 'problem': 'p', 'objects': [], 'observations': [], 'gates': [],
              'experiences': [{'exp_id': 'e', 'change': 'x' * 10000,
                               'evidence': {'exp_id': 'unread'}}],
              'sources': ['experience:e', 'experience:unread']}
    first = read_page([record], user_id='u', limit=1)
    assert first['sources'] == ['experience:e']
    assert first['dependency_sources'] == ['experience:e', 'experience:unread']
    assert 'unread' not in first['items'][0]['text']
    assert first['items'][0]['has_more_text']
    second = read_page([record], user_id='u', limit=1,
                       text_cursor=first['items'][0]['next_text_cursor'])
    assert 'unread' in second['items'][0]['text']
    assert not second['items'][0]['has_more_text']


def test_digest_updates_only_changed_objects_and_tracks_full_input(pool, t0):
    from datetime import timedelta
    from types import SimpleNamespace

    from evowiki.harness.experience import DigestClaim, ObjectDigest, update_object_digests
    from evowiki.store import dreaming, harness_rsi
    from tests.runtime.helpers import make_user

    make_user(pool, "u")
    changes = []
    with pool.connection() as conn:
        for i in range(2):
            rid = harness_rsi.start_round(conn, "u", finding="clarity", now=t0)
            harness_rsi.finish_round(conn, rid, "applied", reason="tested", now=t0)
            cid = harness_rsi.record_change(conn, "u", rid,
                                           {"ops": [{"name": "H1.assistant", "value": str(i)}]},
                                           "applied", now=t0)
            harness_rsi.record_gate(conn, "u", rid, "diff", "pass", [], {}, now=t0)
            changes.append(cid)

    class Gateway:
        def __init__(self):
            self.calls = []

        def call(self, role, request, schema, *, account):
            self.calls.append(request)
            return ObjectDigest(comparisons=[DigestClaim(text="Different settings were tested.",
                                sources=[f"change:{changes[0]}"])]), SimpleNamespace(id=str(len(self.calls)))

    gateway = Gateway()
    assert update_object_digests(pool, "u", gateway, None, now=t0) == 1
    assert update_object_digests(pool, "u", gateway, None, now=t0) == 0
    assert len(gateway.calls) == 1
    assert {f"change:{c}" for c in changes} <= set(gateway.calls[0].params["source_nodes"])
    with pool.connection() as conn:
        prior = dreaming.get(conn, "u", "object", "pack:H1.assistant")
        assert prior and not prior.dirty and len(prior.sources) >= 4
        harness_rsi.add_observation(conn, "u", changes[0], "pending", {}, now=t0 + timedelta(days=1))
    with pool.connection() as conn:
        assert dreaming.get(conn, "u", "object", "pack:H1.assistant").dirty
    assert update_object_digests(pool, "u", gateway, None, now=t0 + timedelta(days=1)) == 1
    assert len(gateway.calls) == 2
