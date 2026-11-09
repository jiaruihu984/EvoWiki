alter table forge_candidate drop constraint forge_candidate_status_check;
alter table forge_candidate add constraint forge_candidate_status_check
  check (status in ('building', 'built', 'build_failed', 'promoted', 'rolled_back', 'withdrawn'));

create table forge_patch (
  candidate_id  text primary key references forge_candidate (id),
  user_id       text references app_user (user_id),
  title         text not null,
  base_commit   text not null,
  commit        text not null,
  branch        text not null,
  state         text not null check (state in ('built', 'awaiting_approval', 'queued', 'landed',
                  'canary_failed', 'revert_queued', 'reverted', 'withdrawn')),
  live_commit   text,
  revert_commit text,
  reason        text not null default '',
  gate_ref      text,
  cost_usd      double precision,
  created_at    timestamptz not null,
  updated_at    timestamptz not null
);
create index forge_patch_state on forge_patch (state, created_at);

create table forge_live_event (
  id           bigserial primary key,
  kind         text not null,          -- land / revert / canary_ok / canary_failed / restart /
                                       -- rebase / withdrawn / revert_all / idle_timeout
  candidate_id text references forge_candidate (id),
  detail       jsonb not null,
  created_at   timestamptz not null
);
create trigger forge_live_event_append_only before update or delete on forge_live_event
  for each row execute function evowiki_forbid_mutation();

create table forge_live_request (
  id         bigserial primary key,
  op         text not null check (op in ('revert_all', 'rebase')),
  by_user    text not null,
  taken      boolean not null default false,
  created_at timestamptz not null
);
