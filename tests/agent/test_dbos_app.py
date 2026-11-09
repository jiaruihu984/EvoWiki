"""DBOS launches against a throwaway Postgres DB with its tables in schema `dbos`, a pinned
application version and JSON serialization; a two-step workflow runs and its steps are recorded."""
import psycopg
import pytest

from evowiki.agent import dbos_app


@pytest.fixture
def dbos_db(db_url):
    yield db_url
    dbos_app.shutdown()


def test_launch_puts_system_tables_in_schema_dbos(dbos_db):
    dbos_app.launch(dbos_db, app_version="b0-test")
    with psycopg.connect(dbos_db) as conn:
        names = {r[0] for r in conn.execute(
            "select table_name from information_schema.tables where table_schema = 'dbos'")}
    assert {"workflow_status", "operation_outputs"} <= names


def test_config_pins_version_and_json():
    cfg = dbos_app.dbos_config("postgresql://u:p@h:1/d", app_version="v7", executor_id="web")
    assert cfg["application_version"] == "v7" and cfg["executor_id"] == "web"
    assert cfg["dbos_system_schema"] == dbos_app.SYSTEM_SCHEMA == "dbos"
    assert cfg["system_database_url"].startswith("postgresql")
    assert cfg["serializer"].name() == "portable_json"          # JSON, never pickle


def test_two_step_workflow_records_steps(dbos_db):
    from dbos import DBOS, SetWorkflowID

    @DBOS.step()
    def add(x: int) -> int:
        return x + 1

    @DBOS.workflow()
    def wf(x: int) -> int:
        return add(add(x))

    dbos_app.launch(dbos_db, app_version="b0-test")
    with SetWorkflowID("t-two-steps"):
        assert wf(1) == 3
    steps = DBOS.list_workflow_steps("t-two-steps")
    assert [s["function_name"].rsplit(".", 1)[-1] for s in steps] == ["add", "add"]


def test_config_accepts_libpq_conninfo():
    cfg = dbos_app.dbos_config("user=postgres dbname=d host=127.0.0.1 port=15", app_version="v1")
    assert cfg["system_database_url"] == "postgresql://postgres@127.0.0.1:15/d"


def test_conninfo_with_unsupported_keys_fails_closed():
    with pytest.raises(ValueError, match="sslmode"):
        dbos_app.dbos_config("user=u dbname=d host=h port=1 sslmode=require", app_version="v1")
    cfg = dbos_app.dbos_config("user=u password=p dbname=d host=h port=1", app_version="v1")
    assert cfg["system_database_url"] == "postgresql://u:p@h:1/d"
