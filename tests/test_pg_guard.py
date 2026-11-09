"""tests/pg.py refuses to run the suite against the shared dev server."""

import pytest

from tests.pg import check_not_dev_server

DEV = "postgresql://u@127.0.0.1:15432/evowiki"


def test_the_dev_servers_port_is_refused():
    with pytest.raises(RuntimeError, match="dev server"):
        check_not_dev_server("postgresql://u@localhost:15432/postgres", None)


def test_the_same_host_and_port_as_the_app_database_is_refused():
    with pytest.raises(RuntimeError, match="dev server"):
        check_not_dev_server("postgresql://x@127.0.0.1:5433/postgres", "postgresql://u@127.0.0.1:5433/evowiki")


def test_a_disposable_server_passes():
    check_not_dev_server("postgresql://x@127.0.0.1:5433/postgres", DEV)
    check_not_dev_server("postgresql://x@127.0.0.1:5433/postgres", None)
