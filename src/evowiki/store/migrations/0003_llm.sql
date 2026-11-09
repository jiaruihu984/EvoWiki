create table llm_call (
  id             text primary key,
  input_hash     text not null unique,
  purpose        text not null,
  prompt_version text not null,
  subject        text not null,
  provider       text not null,
  model          text not null,
  model_version  text,
  params         jsonb not null,
  raw_output     text not null,
  parsed_output  jsonb,
  cost           double precision,
  latency        double precision not null check (latency >= 0),
  ts             timestamptz not null
);

create table llm_attempt (
  id             text primary key,
  input_hash     text not null,
  purpose        text not null,
  prompt_version text not null,
  subject        text not null,
  provider       text not null,
  model          text not null,
  model_version  text,
  params         jsonb not null,
  raw_output     text,
  status         text not null check (status in
                   ('accepted', 'parse_error', 'truncated', 'empty_output', 'refusal',
                    'provider_error')),
  error_type     text,
  error_message  text,
  cost           double precision,
  latency        double precision not null check (latency >= 0),
  ts             timestamptz not null
);
create index llm_attempt_hash on llm_attempt (input_hash);

create trigger llm_call_append_only before update or delete on llm_call
  for each row execute function evowiki_forbid_mutation();
create trigger llm_attempt_append_only before update or delete on llm_attempt
  for each row execute function evowiki_forbid_mutation();
