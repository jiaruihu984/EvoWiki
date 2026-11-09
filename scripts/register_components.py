"""Register the seed interactives (src/evowiki/deep/library_seed.json) into the component library.

  uv run python scripts/register_components.py --dry-run
  uv run python scripts/register_components.py --db-url postgresql://...

Every component is opened in headless Chromium (the render check) first; only passing ones are stored,
a failing one is reported with its reason and never stored. --dry-run runs the checks and stores
nothing. The database needs migration 0027 (the service applies migrations on start). Exit code 1 when
any component failed."""

import argparse
import sys

from evowiki.deep.library_seed import load_seed, register_seed
from evowiki.deep.render import PlaywrightChecker
from evowiki.deep.store import PgLibrary
from evowiki.store.db import make_pool


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db-url", help="Postgres URL of the library to register into")
    ap.add_argument("--dry-run", action="store_true", help="run the render checks, store nothing")
    args = ap.parse_args(argv)
    if bool(args.db_url) == args.dry_run:
        ap.error("give exactly one of --db-url or --dry-run")
    seed = load_seed()
    pool = None if args.dry_run else make_pool(args.db_url)
    try:
        results = register_seed(None if pool is None else PgLibrary(pool), PlaywrightChecker(), seed)
    finally:
        if pool is not None:
            pool.close()
    bad = 0
    for r in results:
        c = r.check
        probes = f"{c.probes_passed}/{c.probes_total}" if c else "-"
        if r.registered:
            print(f"OK    {r.component_id}  probes {probes}" + ("  (dry run)" if pool is None else ""))
        else:
            bad += 1
            print(f"FAIL  {r.component_id}  probes {probes}  {r.error}")
    print(f"{len(results) - bad} passed, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
