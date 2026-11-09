create table wiki_snapshot (
  user_id    text primary key references app_user (user_id),
  wiki_seq   bigint not null,
  reducer    text not null,
  state      jsonb not null,
  updated_at timestamptz not null default now()
);
