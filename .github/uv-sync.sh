#!/usr/bin/env bash
# uv sync for CI; on failure the tail of uv's output becomes an error annotation and the run summary,
# so the reason shows on the run page without opening the step log.
set -o pipefail
uv sync --locked --group dev 2>&1 | tee uv-sync.log
status=$?
if [ "$status" -ne 0 ]; then
  tail_text=$(tail -n 25 uv-sync.log)
  echo "::error title=uv sync --locked failed::${tail_text//$'\n'/%0A}"
  { echo '### uv sync --locked failed'; echo '```'; echo "$tail_text"; echo '```'; } >> "$GITHUB_STEP_SUMMARY"
fi
exit "$status"
