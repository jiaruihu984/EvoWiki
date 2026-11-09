create table rsi_b_round (
  round_id       text primary key,
  user_id        text not null check (user_id <> ''),
  at             timestamptz not null,
  method_version int not null check (method_version >= 1),
  status         text not null check (status in ('no_patch')),
  body           jsonb not null
);
create index rsi_b_round_user on rsi_b_round (user_id, at);
create trigger rsi_b_round_append_only before update or delete on rsi_b_round
  for each row execute function evowiki_forbid_mutation();
create trigger rsi_b_round_no_truncate before truncate on rsi_b_round
  for each statement execute function evowiki_forbid_mutation();
