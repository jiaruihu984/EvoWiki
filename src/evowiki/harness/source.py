"""Where agents get a user's pack: one small read per agent run (or per marked request, through
the gateway's guidance), never cached across runs so a change applies to the next run."""

from collections.abc import Callable

from evowiki.harness.pack import HarnessPack

HarnessSource = Callable[[str], HarnessPack]


def reader(pool) -> HarnessSource:
    from evowiki.store.harness import load_pack

    def read(user_id: str) -> HarnessPack:
        with pool.connection() as conn:
            return load_pack(conn, user_id)
    return read
