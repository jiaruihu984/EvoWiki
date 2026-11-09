alter table llm_call add column user_id text, add column run_id text, add column served_from text;
create index llm_call_user on llm_call (user_id, ts) where user_id is not null;
create index llm_call_run on llm_call (run_id) where run_id is not null;
create index llm_call_served_from on llm_call (served_from) where served_from is not null;

-- The llm_call a usage record paid for (null: a failed call, or a record from before 0090).
alter table usage_record add column call_id text;
create index usage_record_call on usage_record (call_id) where call_id is not null;

-- the agent loop's own agent name for loop runs, see agent/trace.py), `code_version` the code
-- that ran it (EVOWIKI_COMMIT, else the git sha at start). Null on runs from before 0090.
alter table agent_run add column step_key text, add column code_version text;
create index agent_run_step on agent_run (step_key, started_at desc) where step_key is not null;

-- Steps the gateway appends to a traced run (agent/trace.py traced_step, store/trace.py): 'call'
-- = a JSON call (its input is the llm_request of payload.input_call), 'reject' = a reply the step
-- an agent loop is a 'model' step like a loop's, marked payload.via. One step per run and call id:
-- a re-executed workflow body appends nothing twice.
alter table agent_step drop constraint agent_step_kind_check;
alter table agent_step add constraint agent_step_kind_check
  check (kind in ('model', 'tool', 'call', 'reject'));
create unique index agent_step_gateway_call on agent_step (run_id, call_id)
  where call_id is not null and (kind = 'call' or payload ? 'via');
