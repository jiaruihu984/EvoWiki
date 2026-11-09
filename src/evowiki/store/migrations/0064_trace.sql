create table llm_text (
  hash text primary key,
  text text not null
);

create table agent_run (
  run_id       text primary key,
  user_id      text not null references app_user (user_id),
  agent        text not null,
  subject      text not null,
  pack_version integer not null,
  input        jsonb not null,
  started_at   timestamptz not null,
  finished_at  timestamptz,
  stop         text,
  error        text
);
create index agent_run_user on agent_run (user_id, started_at desc);

create table agent_step (
  run_id   text not null references agent_run (run_id),
  seq      integer not null,
  kind     text not null check (kind in ('model', 'tool')),
  payload  jsonb not null,           -- text fields hold llm_text hashes (see agent/trace.py)
  call_id  text,                     -- llm_call.id of a model step
  ts       timestamptz not null,
  primary key (run_id, seq)
);

create table llm_request (
  call_id     text primary key,
  system_hash text not null references llm_text (hash),
  prompt_hash text not null references llm_text (hash),
  schema_name text not null
);
