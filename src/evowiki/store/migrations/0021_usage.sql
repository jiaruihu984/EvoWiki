create table usage_record (
  id            bigint generated always as identity primary key,
  user_id       text not null check (user_id <> ''),
  grp           text not null check (grp in ('page', 'assistant', 'curator', 'diagnostic',
                                             'review', 'other')),
  purpose       text not null,
  provider      text not null,
  model         text not null,
  input_tokens  bigint not null check (input_tokens >= 0),
  output_tokens bigint not null check (output_tokens >= 0),
  cost_usd      double precision check (cost_usd is null or cost_usd >= 0),
  replayed      boolean not null,
  ts            timestamptz not null
);
create index usage_record_user_ts on usage_record (user_id, ts);
create trigger usage_record_append_only before update or delete on usage_record
  for each row execute function evowiki_forbid_mutation();
create trigger usage_record_no_truncate before truncate on usage_record
  for each statement execute function evowiki_forbid_mutation();

-- Per-user limits (null = none) and tier overrides ({group: tier}; missing = default tier).
create table usage_settings (
  user_id           text primary key check (user_id <> ''),
  daily_limit_usd   double precision check (daily_limit_usd is null or daily_limit_usd >= 0),
  monthly_limit_usd double precision check (monthly_limit_usd is null or monthly_limit_usd >= 0),
  tiers             jsonb not null default '{}'::jsonb,
  updated_at        timestamptz not null
);
