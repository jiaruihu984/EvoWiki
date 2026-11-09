create table assistant_session (
  session_id      text primary key,
  user_id         text not null references app_user (user_id),
  page_id         text,
  note_concept_id text,
  created_at      timestamptz not null
);
create table assistant_entry (
  session_id  text not null references assistant_session (session_id),
  seq         bigint not null check (seq >= 1),
  entry_id    text not null unique,
  type        text not null check (type in ('user', 'tool', 'assistant', 'proposal', 'decision',
                                            'observed', 'compaction', 'turn_error')),
  payload     jsonb not null,
  occurred_at timestamptz not null,
  ingested_at timestamptz not null default clock_timestamp(),
  primary key (session_id, seq)
);
create unique index assistant_entry_proposal on assistant_entry ((payload ->> 'proposal_id'))
  where type = 'proposal';
create index assistant_entry_decision on assistant_entry ((payload ->> 'proposal_id'))
  where type = 'decision';
create trigger assistant_session_append_only before update or delete on assistant_session
  for each row execute function evowiki_forbid_mutation();
create trigger assistant_session_no_truncate before truncate on assistant_session
  for each statement execute function evowiki_forbid_mutation();
create trigger assistant_entry_append_only before update or delete on assistant_entry
  for each row execute function evowiki_forbid_mutation();
create trigger assistant_entry_no_truncate before truncate on assistant_entry
  for each statement execute function evowiki_forbid_mutation();
