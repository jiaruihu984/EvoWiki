create table wiki_event (
  user_id     text not null references app_user (user_id),
  seq         bigint not null check (seq >= 1),
  event_id    text not null unique,
  occurred_at timestamptz not null,
  type        text not null,
  payload     jsonb not null,
  ingested_at timestamptz not null default clock_timestamp(),
  primary key (user_id, seq)
);
create trigger wiki_event_append_only before update or delete on wiki_event
  for each row execute function evowiki_forbid_mutation();
create trigger wiki_event_no_truncate before truncate on wiki_event
  for each statement execute function evowiki_forbid_mutation();

-- shared written-section cache (goals.writer.SectionCache); newest row per key wins
create table section_cache (
  id         bigserial primary key,
  key        text not null,
  text       text not null,
  created_at timestamptz not null default clock_timestamp()
);
create index section_cache_key on section_cache (key, id desc);
create trigger section_cache_append_only before update or delete on section_cache
  for each row execute function evowiki_forbid_mutation();
create trigger section_cache_no_truncate before truncate on section_cache
  for each statement execute function evowiki_forbid_mutation();
