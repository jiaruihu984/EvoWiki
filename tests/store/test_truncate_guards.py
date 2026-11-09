import psycopg
import pytest

APPEND_ONLY = ["extractor_version",
               "llm_call", "llm_attempt", "content_snapshot", "policy_version",
               "aspect_event", "wiki_event", "rsi_policy_version", "rsi_cycle"]


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_truncate_is_forbidden_on_append_only_tables(db_url, table):
    with psycopg.connect(db_url) as conn:
        with pytest.raises(psycopg.errors.RaiseException, match=f"append-only table {table}: TRUNCATE"):
            conn.execute(f"truncate {table} cascade")
        conn.rollback()
