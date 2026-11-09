create table otel_export (
  trace_id       text primary key check (trace_id ~ '^[0-9a-f]{32}$'),
  user_id        text not null,
  session_key    text,
  run_started_at timestamptz not null,   -- agent_run.started_at: finds the run again by index
  state          text not null check (state in ('sent', 'exported')),
  exported_at    timestamptz not null
);
create index otel_export_user on otel_export (user_id, run_started_at);
create index otel_export_session on otel_export (session_key) where session_key is not null;
