create table policy_version (
  id             text primary key,
  scope          text not null check (scope in ('global', 'personal')),
  user_id        text references app_user (user_id),
  parent_id      text references policy_version (id),
  theta_or_delta jsonb not null,
  schema_version text not null,
  created_at     timestamptz not null,
  check ((scope = 'personal') = (user_id is not null))
);
create trigger policy_version_append_only before update or delete on policy_version
  for each row execute function evowiki_forbid_mutation();

create table belief_state (
  user_id           text primary key references app_user (user_id),
  policy_version    text not null references policy_version (id),
  extractor_version text not null,
  content_version   text not null references content_snapshot (id),
  ledger_offset     bigint not null check (ledger_offset >= 0),
  ambiguous         text[] not null,
  applied           int not null check (applied >= 0),
  updated_at        timestamptz not null
);

create table belief (
  user_id        text not null references belief_state (user_id) on delete cascade,
  concept_id     text not null,
  alpha          double precision not null check (alpha > 0 and alpha < 'Infinity'::float8),
  beta           double precision not null check (beta > 0 and beta < 'Infinity'::float8),
  stability_s    double precision not null
                   check (stability_s > 0 and stability_s < 'Infinity'::float8),
  last_recall_ts timestamptz,
  evidence_count int not null check (evidence_count >= 0),
  primary key (user_id, concept_id)
);
