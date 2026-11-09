create table forge_candidate (
  id         text primary key,
  user_id    text references app_user (user_id),       -- null: global scope
  kind       text not null check (kind in ('tool', 'patch')),
  name       text,                                      -- tool name (kind = tool)
  parent_id  text references forge_candidate (id),
  spec       jsonb not null,
  evidence   jsonb not null,
  files      jsonb not null default '{}'::jsonb,        -- {path: text}
  build      jsonb,                                     -- BuildReport
  status     text not null check (status in ('building', 'built', 'build_failed', 'promoted',
                                             'rolled_back')),
  run_id     text,
  cost_usd   double precision,
  created_at timestamptz not null,
  updated_at timestamptz not null
);
create index forge_candidate_user on forge_candidate (user_id, created_at desc);

create table forge_registry (                           -- append-only; head = max(version) per scope
  scope      text not null,                             -- user id, or 'global'
  version    integer not null,
  entries    jsonb not null,                            -- {tool_name: candidate_id}
  change     jsonb not null,                            -- {op, candidate_id?, of_change?, by, why, gate_ref?}
  created_at timestamptz not null,
  primary key (scope, version)
);

create trigger forge_registry_append_only before update or delete on forge_registry
  for each row execute function evowiki_forbid_mutation();

create table forge_setting (
  scope      text not null,
  key        text not null,
  value      jsonb not null,
  updated_at timestamptz not null,
  primary key (scope, key)
);
