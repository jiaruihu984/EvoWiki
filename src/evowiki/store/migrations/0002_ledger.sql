create table app_user (
  user_id    text primary key check (user_id ~ '^[A-Za-z0-9_.-]{1,64}$'),
  background text not null,
  condition  text not null check (condition in ('S0', 'S1', 'S2', 'S3')),
  lang       text not null check (lang in ('en', 'zh')),
  created_at timestamptz not null
);

create table user_session (
  session_id text primary key,
  user_id    text not null references app_user (user_id),
  started_at timestamptz not null,
  ended_at   timestamptz,
  check (ended_at is null or ended_at >= started_at)
);

create table event (
  user_id        text not null references app_user (user_id),
  seq            bigint not null check (seq >= 1),
  event_id       text not null unique,
  session_id     text not null references user_session (session_id),
  occurred_at    timestamptz not null,               -- observed metadata (Event.ts); may go backwards
  ingested_at    timestamptz not null default clock_timestamp(),  -- server time at append
  type           text not null,
  payload        jsonb not null,
  render_plan_id text,
  primary key (user_id, seq)                          -- seq = causal log order (decision 1)
);
create index event_session on event (session_id, seq);

create trigger event_append_only before update or delete on event
  for each row execute function evowiki_forbid_mutation();
create trigger event_no_truncate before truncate on event
  for each statement execute function evowiki_forbid_mutation();
