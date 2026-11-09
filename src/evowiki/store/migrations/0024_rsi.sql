create table rsi_policy_version (
  user_id    text not null check (user_id <> ''),
  track      text not null check (track in ('A', 'B')),
  version    int not null check (version >= 1),
  at         timestamptz not null,
  patch_id   text,
  body       jsonb not null,
  primary key (user_id, track, version)
);
create trigger rsi_policy_version_append_only before update or delete on rsi_policy_version
  for each row execute function evowiki_forbid_mutation();
create trigger rsi_policy_version_no_truncate before truncate on rsi_policy_version
  for each statement execute function evowiki_forbid_mutation();

-- Patches (mutable: status moves through the §7.5 lifecycle). body = the whole Patch.
-- updated_at is nullable: PatchRecord.updated_at is None until the first gate/status update.
create table rsi_patch (
  patch_id           text primary key,
  user_id            text not null check (user_id <> ''),
  track              text not null,
  status             text not null,
  created_at         timestamptz not null,
  body               jsonb not null,
  cutoff             timestamptz,
  gate_runs          int not null default 0,
  last_gate_outcomes int not null default 0,
  updated_at         timestamptz
);
create index rsi_patch_user on rsi_patch (user_id, created_at);

-- Patch Memory (§7.6): one row per closed patch; online counts decay, so rows are upserted.
-- entry = the whole PatchMemoryEntry. Track B rows: direction 0, from/to null.
create table rsi_memory (
  patch_id           text primary key,
  user_id            text not null check (user_id <> ''),
  track              text not null,
  signature          text not null,
  path               text not null,
  direction          smallint not null,
  entry              jsonb not null,
  from_value         double precision,
  to_value           double precision,
  counts_at          timestamptz not null,
  repeat_of_rejected boolean not null,
  outcomes_at_close  int not null,
  complexity         int not null check (complexity >= 0),
  at                 timestamptz not null
);
create index rsi_memory_user on rsi_memory (user_id, at);

-- Cycle log: one row per RSI cycle run. Append-only.
create table rsi_cycle (
  id       bigint generated always as identity primary key,
  user_id  text not null check (user_id <> ''),
  at       timestamptz not null,
  trigger  text not null check (trigger in ('manual', 'every_5_feedback', 'weekly')),
  result   jsonb not null,
  cost_usd double precision not null check (cost_usd >= 0)
);
create index rsi_cycle_user on rsi_cycle (user_id, at);
create trigger rsi_cycle_append_only before update or delete on rsi_cycle
  for each row execute function evowiki_forbid_mutation();
create trigger rsi_cycle_no_truncate before truncate on rsi_cycle
  for each statement execute function evowiki_forbid_mutation();

-- Per-user RSI settings (a missing row = RsiSettings() defaults).
create table rsi_settings (
  user_id    text primary key check (user_id <> ''),
  settings   jsonb not null,
  updated_at timestamptz not null
);
