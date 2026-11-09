create table aspect_event (
  user_id     text not null references app_user (user_id),
  seq         bigint not null check (seq >= 1),
  event_id    text not null unique,
  occurred_at timestamptz not null,
  type        text not null check (type in ('define_concept', 'observe', 'revoke_inference',
                                            'declare', 'add_note', 'retire_note')),
  payload     jsonb not null,
  ingested_at timestamptz not null default clock_timestamp(),
  primary key (user_id, seq)
);
create trigger aspect_event_append_only before update or delete on aspect_event
  for each row execute function evowiki_forbid_mutation();
create trigger aspect_event_no_truncate before truncate on aspect_event
  for each statement execute function evowiki_forbid_mutation();
