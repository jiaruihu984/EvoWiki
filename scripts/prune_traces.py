"""Apply the trace retention policy now (store/retention.py, migration 0092). The service runs the
same job daily (service/retention_job.py); this is for a manual run or a dry run.

  uv run python scripts/prune_traces.py [--env .env] [--dry-run]

The policy comes from the env file (EVOWIKI_RETENTION_TEXT_DAYS / _META_DAYS / _SIGNAL_TEXT_DAYS,
defaults 90 / 180 / 180). --dry-run does the whole run and rolls it back, printing what it would do."""

import argparse
from datetime import UTC, datetime
from pathlib import Path

from envfile import read_env_file

from evowiki.store import retention
from evowiki.store.db import database_url, make_pool


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--env", type=Path, default=Path(__file__).resolve().parents[1] / ".env")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    env = read_env_file(args.env)
    policy = retention.RetentionPolicy.from_env(env)
    pool = make_pool(database_url(env), min_size=1, max_size=2)
    try:
        with pool.connection() as conn:
            rep = retention.run(conn, policy, now=datetime.now(UTC), requested_by="script:prune_traces",
                                dry_run=args.dry_run)
    finally:
        pool.close()
    print(f"{'dry run' if args.dry_run else 'retention run ' + rep.run_id}: texts before "
          f"{rep.text_cutoff:%Y-%m-%d}, metadata before {rep.meta_cutoff:%Y-%m-%d}")
    for step, rows in sorted(rep.counts.items()):
        print(f"  {step}: {rows}")


if __name__ == "__main__":
    main()
