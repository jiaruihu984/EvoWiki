import pytest

from evowiki.forge import ids, switch
from evowiki.forge.registry import view


@pytest.mark.parametrize("bad", ["global", "", " "])
def test_check_user_refuses(bad):
    with pytest.raises(ValueError):
        ids.check_user(bad)


def test_entry_points_refuse_global(pool):
    with pool.connection() as conn:
        with pytest.raises(ValueError):
            view(conn, "global", enabled=True)
        with pytest.raises(ValueError):
            switch.enabled(conn, "global", "tool")
