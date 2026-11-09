create table rsi_outcome_point (
  id         bigint generated always as identity primary key,
  user_id    text not null check (user_id <> ''),
  change_id  text not null check (change_id <> ''),
  at         timestamptz not null,
  verdict    text not null check (verdict in ('pending', 'helpful', 'harmful', 'no_effect')),
  body       jsonb not null
);
create index rsi_outcome_point_change on rsi_outcome_point (user_id, change_id, at);
create trigger rsi_outcome_point_append_only before update or delete on rsi_outcome_point
  for each row execute function evowiki_forbid_mutation();
create trigger rsi_outcome_point_no_truncate before truncate on rsi_outcome_point
  for each statement execute function evowiki_forbid_mutation();
