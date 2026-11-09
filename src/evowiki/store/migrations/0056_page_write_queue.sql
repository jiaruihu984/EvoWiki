drop index if exists page_run_one_prefetch;

-- when the workflow actually began (before that: queued), what the live status shows while it runs,
-- what it cost, and why it failed (3 idle failures of one kind in a day stop the idle writing)
alter table page_run
    add column began_at   timestamptz,
    add column progress   jsonb,
    add column cost_usd   double precision,
    add column llm_calls  integer,
    add column failure    text;
create index page_run_user_live on page_run (user_id, started_at desc);
