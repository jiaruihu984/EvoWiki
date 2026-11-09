create table memory_episode (
  user_id           text not null references app_user (user_id),
  session_id        text not null references assistant_session (session_id),
  message_id        text not null,           -- the user message of the turn
  answer_message_id text not null,
  trace_run_id      text not null,           -- assistant:{session}:{turn}
  turn_index        int not null,            -- index into SessionState.turns
  page_id           text,
  concept_ids       text[] not null default '{}',
  at                timestamptz not null,
  user_text         text not null,
  answer_text       text not null,
  embedder_id       text, model_version text, dim int,
  embedding         vector,
  primary key (session_id, message_id),
  check ((embedding is null) = (embedder_id is null)),
  check (embedding is null or vector_dims(embedding) = dim)
);
create index memory_episode_user_at on memory_episode (user_id, at desc);

create table memory_item (
  item_id        text primary key,
  user_id        text not null references app_user (user_id),
  kind           text not null check (kind in ('preference','about_me','decision','open_question')),
  content        text not null,
  provenance     text not null check (provenance in
                   ('explicit_current','explicit_history','observed','inferred')),
  evidence       jsonb not null,             -- EvidenceRef[]
  scope_type     text not null check (scope_type in ('global','goal','page','concept')),
  scope_id       text,
  page_id        text,
  concept_ids    text[] not null default '{}',
  session_id     text not null,              -- the session it was consolidated from
  valid_from     timestamptz not null,
  created_at     timestamptz not null,
  status         text check (status in ('active','superseded','resolved')),  -- null for preference/about_me
  superseded_by  text references memory_item (item_id) on delete set null,
  status_history jsonb not null default '[]',   -- [{status, at, by, superseded_by}], append-only; status is the latest
  spec_item_id   text, profile_ref jsonb, pending_action_id text,
  embedder_id    text, model_version text, dim int, embedding vector,
  check ((kind in ('preference','about_me')) = (status is null)),
  check ((scope_type = 'global') = (scope_id is null)),
  check ((embedding is null) = (embedder_id is null)),
  check (embedding is null or vector_dims(embedding) = dim)
);
create index memory_item_user_kind on memory_item (user_id, kind, status);
create index memory_item_session on memory_item (session_id);

-- One forgotten memory item (plan Task 13 writes it): ids, kind and scope only, never its text,
-- so consolidating the same turns again does not bring it back.
create table memory_suppression (
  user_id              text not null,
  item_id              text primary key,
  kind                 text not null,
  evidence_message_ids text[] not null,
  scope_type           text not null,
  scope_id             text,
  at                   timestamptz not null
);
create index memory_suppression_user on memory_suppression (user_id);

-- A deleted session (plan Task 13). No foreign key: it outlives the session row.
create table forgotten_session (
  user_id    text not null,
  session_id text primary key,
  at         timestamptz not null
);

-- verify_quote / read_turn look a message up by id
create index assistant_entry_message on assistant_entry ((payload ->> 'message_id'))
  where type in ('user', 'assistant');

alter table assistant_entry drop constraint if exists assistant_entry_type_check;
alter table assistant_entry add constraint assistant_entry_type_check
  check (type in ('user', 'tool', 'assistant', 'proposal', 'decision', 'observed', 'compaction',
                  'turn_error', 'rating', 'consolidation'));
