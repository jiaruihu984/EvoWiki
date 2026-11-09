create table extractor_version (
  id             text primary key,
  kind           text not null check (kind in ('rule', 'llm')),
  prompt_version text,
  created_at     timestamptz not null,
  notes          text not null default ''
);

create table extraction_job (
  event_id          text not null references event (event_id),
  extractor_version text not null references extractor_version (id),
  user_id           text not null,
  seq               bigint not null,
  status            text not null
                      check (status in ('pending', 'running', 'done', 'failed', 'dead')),
  attempts          int not null default 0 check (attempts >= 0),
  available_at      timestamptz not null,
  locked_until      timestamptz,
  last_error        text,
  updated_at        timestamptz not null,
  idempotency_key   text generated always as (event_id || ':' || extractor_version) stored
                      unique,
  primary key (event_id, extractor_version),
  foreign key (user_id, seq) references event (user_id, seq)
);
create index extraction_job_claim on extraction_job (status, available_at);
create trigger extraction_job_no_delete before delete on extraction_job
  for each row execute function evowiki_forbid_mutation();

create table evidence (
  id                    text primary key,
  user_id               text not null,
  seq                   bigint not null,
  ordinal               int not null check (ordinal >= 0),
  source_event_id       text not null references event (event_id),
  extractor_version     text not null references extractor_version (id),
  llm_call_id           text references llm_call (id),
  concept_id            text not null,
  kc_hint               text,
  evidence_type         text not null,
  polarity              text not null check (polarity in ('positive', 'negative')),
  extraction_confidence double precision not null
                          check (extraction_confidence >= 0 and extraction_confidence <= 1),
  ts                    timestamptz not null,
  unique (source_event_id, extractor_version, ordinal),
  foreign key (user_id, seq) references event (user_id, seq)
);
create index evidence_user_order on evidence (user_id, seq, extractor_version, ordinal);

create table extraction_output (
  event_id          text not null references event (event_id),
  extractor_version text not null references extractor_version (id),
  llm_call_id       text references llm_call (id),
  output            jsonb not null,
  created_at        timestamptz not null,
  primary key (event_id, extractor_version)
);

create trigger evidence_append_only before update or delete on evidence
  for each row execute function evowiki_forbid_mutation();
create trigger extraction_output_append_only before update or delete on extraction_output
  for each row execute function evowiki_forbid_mutation();
create trigger extractor_version_append_only before update or delete on extractor_version
  for each row execute function evowiki_forbid_mutation();
