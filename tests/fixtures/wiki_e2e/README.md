# wiki_e2e: scripted LLM for the z-score → PCA scenario

**Synthetic, scripted, non-reportable.** `script.json` is hand-written test data, not a recording
of any real LLM. No model produced it and no number from a run over it means anything about
model quality. Never report results from it.

- Format: `{purpose: [output, ...]}`. Each request purpose (`goals.intake` — the goal's outline,
  read as one layer by tests/wikiapp/flat_goal.py through goals.enter, with its `known` estimate
  from the description (S2: the separate gap call is gone) —
  `goals.probe`, `goals.diagnostic.verify`, `goals.path`, `plan_page` (the writer's outline, taken by the scripted writer),
  `goals.write`) answers from its own queue, in order. `tests/wikiapp/e2e_driver.load_script`
  loads it into `tests/wikiapp/script.Script`, which puts a `ScriptedProvider` behind a
  record-mode Gateway over an in-memory call log. An unscripted call fails the test.
- Used by `tests/wikiapp/test_e2e_zscore_pca.py` (assertions on structured outputs) and
  `tests/wikiapp/test_replay.py` (the MemoryRepo run equals the PgRepo run, and replay equals the
  runtime state).
- The data is fitted to the flow, not to a result. `goals.probe` is not a queue but a bank of
  quizzes: the KST diagnosis (goals/probe.py, goals/kst.py) picks the node each round, and
  `e2e_driver.bank_writer` answers with the first bank quiz on one of that node's cells (anything
  else is written generically). The probe shuffles every quiz deterministically
  (`goals.diagnostic._shuffled`); the driver answers with the shuffled key. The path model keeps
  each goal on one page; page ids (`pg_0001` / `pg_0002`) come from the driver's per-prefix
  `Ids`. When a flow
  changes, fix this script; never change product behaviour to make the script fit.

## Recording a real run later

A real run must not edit this file. Record into this directory with
`tests/llm_fixtures.fixture_gateway(Path("tests/fixtures/wiki_e2e"))` under
`EVOWIKI_RECORD_LIVE=1`, using synthetic inputs only. That writes `llm_calls.jsonl` (plus
`llm_attempts.jsonl` and `MANIFEST.json`) next to this README. Later runs replay
`llm_calls.jsonl` by default. Once `llm_calls.jsonl` exists,
`tests/test_llm_fixture_manifests.py` checks it. That test looks only at `llm_calls.jsonl`, so
`script.json` is not checked. The pinned recording config is DeepSeek single-provider, so a
recording made that way is marked `reportable: false` as well.
