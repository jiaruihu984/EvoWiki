create table reconcile_report (
  id      bigserial primary key,
  run_id  text not null,
  user_id text not null references app_user (user_id),
  status  text not null check (status in ('ok', 'mismatch', 'stale', 'missing')),
  detail  jsonb not null,
  ts      timestamptz not null
);
create index reconcile_report_run on reconcile_report (run_id);
create trigger reconcile_report_append_only before update or delete on reconcile_report
  for each row execute function evowiki_forbid_mutation();
