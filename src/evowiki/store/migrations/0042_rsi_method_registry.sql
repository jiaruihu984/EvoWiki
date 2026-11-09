create table rsi_method_version (
  user_id    text not null check (user_id <> ''),
  kind       text not null check (kind in ('A2', 'B2')),
  version    int not null check (version >= 1),
  at         timestamptz not null,
  change_id  text,
  body       jsonb not null,
  primary key (user_id, kind, version)
);
create trigger rsi_method_version_append_only before update or delete on rsi_method_version
  for each row execute function evowiki_forbid_mutation();
create trigger rsi_method_version_no_truncate before truncate on rsi_method_version
  for each statement execute function evowiki_forbid_mutation();

-- rsi_method_change: one row per layer-2 change (mutable: applied → rolled_back). body = the whole
-- MethodChange; updated_at is null until the first status update.
create table rsi_method_change (
  change_id  text primary key,
  user_id    text not null check (user_id <> ''),
  kind       text not null check (kind in ('A2', 'B2')),
  status     text not null check (status in ('applied', 'gate_failed', 'rolled_back')),
  created_at timestamptz not null,
  updated_at timestamptz,
  body       jsonb not null
);
create index rsi_method_change_user on rsi_method_change (user_id, created_at);
