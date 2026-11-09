alter table llm_call add column replayable boolean not null default true;

alter table llm_call drop constraint llm_call_input_hash_key;   -- fails loudly if the name differs
create unique index llm_call_replay_key on llm_call (input_hash) where replayable;
create index llm_call_hash on llm_call (input_hash);            -- audit lookup of all executions

-- A non-replayable execution can never occupy the canonical cache id llm_<hash[:20]> (false provenance).
alter table llm_call add constraint llm_call_execution_id_distinct
  check (replayable or id <> 'llm_' || left(input_hash, 20));
