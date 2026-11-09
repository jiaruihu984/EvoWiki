alter table llm_call add column text_expired_at timestamptz;
alter table llm_attempt add column text_expired_at timestamptz;
alter table agent_run add column text_expired_at timestamptz;

alter table llm_call drop constraint llm_call_execution_id_distinct;
alter table llm_call add constraint llm_call_execution_id_distinct
  check (replayable or text_expired_at is not null or id <> 'llm_' || left(input_hash, 20));

-- ---------- runs and audit ----------
create table retention_run (
  run_id       text primary key,
  requested_by text not null,
  policy       jsonb not null,
  text_cutoff  timestamptz not null,       -- a failed run: the cutoffs it asked for
  meta_cutoff  timestamptz not null,
  state        text not null check (state in ('running', 'committed', 'failed')),
  started_at   timestamptz not null,
  updated_at   timestamptz not null,
  error        text,
  opened_xid   xid8,
  check (meta_cutoff <= text_cutoff)
);

create table retention_audit (
  id     bigserial primary key,
  run_id text not null references retention_run (run_id),
  at     timestamptz not null default clock_timestamp(),
  step   text not null,
  rows   integer not null
);
create index retention_audit_run on retention_audit (run_id, id);

alter table retention_run owner to evowiki_forget_owner;
alter table retention_audit owner to evowiki_forget_owner;
revoke all on retention_run, retention_audit from public, evowiki_app;
grant select on retention_run, retention_audit to evowiki_app;
revoke all on sequence retention_audit_id_seq from public, evowiki_app;

-- ---------- the gate (SECURITY DEFINER, owned by evowiki_forget_owner) ----------
-- No SET clause (it would undo set_config at function exit); every name schema-qualified, as 0082.

create function evowiki_retaining() returns boolean
language plpgsql security definer as $$
declare rid text := pg_catalog.current_setting('evowiki.retention', true);
begin
  if rid is null or rid = '' then
    return false;
  end if;
  return exists (select from public.retention_run r
                  where r.run_id = rid and r.state = 'running'
                    and r.opened_xid = pg_catalog.pg_current_xact_id());
end $$;

-- The open run's cutoffs (null, null when the gate is closed).
create function evowiki_retention_cutoffs(out text_cutoff timestamptz, out meta_cutoff timestamptz)
language plpgsql security definer as $$
begin
  if not public.evowiki_retaining() then
    return;
  end if;
  select r.text_cutoff, r.meta_cutoff into text_cutoff, meta_cutoff from public.retention_run r
   where r.run_id = pg_catalog.current_setting('evowiki.retention', true);
end $$;

create function evowiki_retention_open(p_requested_by text, p_policy jsonb, p_text_cutoff timestamptz,
                                       p_meta_cutoff timestamptz) returns text
language plpgsql security definer as $$
declare
  rid text := pg_catalog.gen_random_uuid()::text;
  floor_at timestamptz := pg_catalog.now() - interval '30 days';   -- RETENTION_FLOOR
begin
  if p_requested_by is null or p_requested_by = '' or p_policy is null
     or p_text_cutoff is null or p_meta_cutoff is null then
    raise exception 'retention run needs who requested it, its policy and both cutoffs';
  end if;
  if p_text_cutoff > floor_at or p_meta_cutoff > floor_at then
    raise exception 'retention cutoff must be at least 30 days ago (text %, meta %)',
      p_text_cutoff, p_meta_cutoff;
  end if;
  if p_meta_cutoff > p_text_cutoff then
    raise exception 'retention: metadata cannot expire before the texts (meta % > text %)',
      p_meta_cutoff, p_text_cutoff;
  end if;
  if public.evowiki_forgetting() or public.evowiki_retaining() then
    raise exception 'retention: another gate is open in this transaction';
  end if;
  insert into public.retention_run (run_id, requested_by, policy, text_cutoff, meta_cutoff, state,
                                    started_at, updated_at, opened_xid)
  values (rid, p_requested_by, p_policy, p_text_cutoff, p_meta_cutoff, 'running', pg_catalog.now(),
          pg_catalog.now(), pg_catalog.pg_current_xact_id());
  perform pg_catalog.set_config('evowiki.retention', rid, true);
  insert into public.retention_audit (run_id, step, rows) values (rid, 'open', 0);
  return rid;
end $$;

create function evowiki_retention_close(p_run_id text) returns void
language plpgsql security definer as $$
begin
  if pg_catalog.current_setting('evowiki.retention', true) is distinct from p_run_id
     or not public.evowiki_retaining() then
    raise exception 'retention run % is not open in this transaction', p_run_id;
  end if;
  update public.retention_run set state = 'committed', updated_at = pg_catalog.now()
   where run_id = p_run_id;
  insert into public.retention_audit (run_id, step, rows) values (p_run_id, 'close', 0);
  perform pg_catalog.set_config('evowiki.retention', '', true);
end $$;

-- A run whose transaction rolled back left no row: record the failure (its own transaction) so the
-- attempt is visible next to the committed runs.
create function evowiki_retention_failed(p_requested_by text, p_policy jsonb, p_text_cutoff timestamptz,
                                         p_meta_cutoff timestamptz, p_error text) returns text
language plpgsql security definer as $$
declare rid text := pg_catalog.gen_random_uuid()::text;
begin
  if public.evowiki_retaining() then
    raise exception 'retention gate is open in this transaction; roll it back first';
  end if;
  insert into public.retention_run (run_id, requested_by, policy, text_cutoff, meta_cutoff, state,
                                    started_at, updated_at, error)
  values (rid, coalesce(p_requested_by, ''), coalesce(p_policy, '{}'::jsonb), p_text_cutoff,
          p_meta_cutoff, 'failed', pg_catalog.now(), pg_catalog.now(), p_error);
  insert into public.retention_audit (run_id, step, rows) values (rid, 'fail', 0);
  return rid;
end $$;

-- A step on a table without audit triggers (or a count worth naming): audited by name.
create function evowiki_retention_note(p_run_id text, p_step text, p_rows integer) returns void
language plpgsql security definer as $$
begin
  if pg_catalog.current_setting('evowiki.retention', true) is distinct from p_run_id
     or not public.evowiki_retaining() then
    raise exception 'retention run % is not open in this transaction', p_run_id;
  end if;
  insert into public.retention_audit (run_id, step, rows) values (p_run_id, p_step, p_rows);
end $$;

-- 0082's evowiki_forget_open, refusing while the retention gate is open.
create or replace function evowiki_forget_open(p_request_id text) returns void
language plpgsql security definer as $$
declare st text;
begin
  if public.evowiki_retaining() then
    raise exception 'forget request %: the retention gate is open in this transaction', p_request_id;
  end if;
  select r.state into st from public.forget_request r where r.request_id = p_request_id for update;
  if st is null then
    raise exception 'forget request % does not exist', p_request_id;
  end if;
  if st not in ('requested', 'failed') then
    raise exception 'forget request %: cannot open from state %', p_request_id, st;
  end if;
  update public.forget_request
     set state = 'deleting', opened_xid = pg_catalog.pg_current_xact_id(), updated_at = pg_catalog.now(),
         error = null
   where request_id = p_request_id;
  perform pg_catalog.set_config('evowiki.forget', p_request_id, true);
  insert into public.forget_audit (request_id, step, rows) values (p_request_id, 'open', 0);
end $$;

-- 0082's statement audit, writing retention_audit while the retention gate is open.
create or replace function evowiki_forget_audit_rows() returns trigger
language plpgsql security definer as $$
declare
  n integer;
  forgetting boolean := coalesce(pg_catalog.current_setting('evowiki.forget', true), '') <> ''
                        and public.evowiki_forgetting();
  retaining boolean := coalesce(pg_catalog.current_setting('evowiki.retention', true), '') <> ''
                       and public.evowiki_retaining();
begin
  if not forgetting and not retaining then
    return null;
  end if;
  if TG_OP = 'DELETE' then
    select pg_catalog.count(*) into n from forget_old;
  else
    select pg_catalog.count(*) into n from forget_new;
  end if;
  if n > 0 and forgetting then
    insert into public.forget_audit (request_id, step, rows)
    values (pg_catalog.current_setting('evowiki.forget', true),
            pg_catalog.lower(TG_OP) || ':' || TG_TABLE_NAME, n);
  elsif n > 0 then
    insert into public.retention_audit (run_id, step, rows)
    values (pg_catalog.current_setting('evowiki.retention', true),
            pg_catalog.lower(TG_OP) || ':' || TG_TABLE_NAME, n);
  end if;
  return null;
end $$;

do $$
declare f text;
begin
  foreach f in array array['evowiki_retaining()', 'evowiki_retention_cutoffs()',
                           'evowiki_retention_open(text, jsonb, timestamptz, timestamptz)',
                           'evowiki_retention_close(text)',
                           'evowiki_retention_failed(text, jsonb, timestamptz, timestamptz, text)',
                           'evowiki_retention_note(text, text, integer)'] loop
    execute format('alter function %s owner to evowiki_forget_owner', f);
    execute format('revoke all on function %s from public', f);
    execute format('grant execute on function %s to evowiki_app', f);
  end loop;
end $$;

-- ---------- the guarded tables ----------
-- Every guarded table here has a `ts` column: the row's own time is what the cutoffs bound.
create function evowiki_retain_guard() returns trigger
language plpgsql as $$
declare
  c record;
  same boolean := TG_OP = 'UPDATE' and TG_NARGS > 0
                  and (to_jsonb(old) - TG_ARGV::text[]) = (to_jsonb(new) - TG_ARGV::text[]);
begin
  if public.evowiki_forgetting() then
    if TG_OP = 'DELETE' then
      return old;
    end if;
    if same then
      return new;
    end if;
  elsif public.evowiki_retaining() then
    select * into c from public.evowiki_retention_cutoffs();
    if TG_OP = 'DELETE' and old.ts < c.meta_cutoff then
      return old;
    end if;
    if same and old.ts < c.text_cutoff then
      return new;
    end if;
  end if;
  raise exception 'append-only table %: % is forbidden', TG_TABLE_NAME, TG_OP;
end $$;

drop trigger llm_call_append_only on llm_call;
create trigger llm_call_append_only before update or delete on llm_call
  for each row execute function evowiki_retain_guard('raw_output', 'parsed_output', 'replayable',
                                                     'text_expired_at');
drop trigger llm_attempt_append_only on llm_attempt;
create trigger llm_attempt_append_only before update or delete on llm_attempt
  for each row execute function evowiki_retain_guard('raw_output', 'error_message', 'text_expired_at');
-- usage_record holds no text and the forget flow never deletes it: only the retention gate may, and
-- only a DELETE of a row before the meta cutoff.
create function evowiki_retain_delete_guard() returns trigger
language plpgsql as $$
begin
  if TG_OP = 'DELETE' and public.evowiki_retaining()
     and old.ts < (select c.meta_cutoff from public.evowiki_retention_cutoffs() c) then
    return old;
  end if;
  raise exception 'append-only table %: % is forbidden', TG_TABLE_NAME, TG_OP;
end $$;

drop trigger usage_record_append_only on usage_record;
create trigger usage_record_append_only before update or delete on usage_record
  for each row execute function evowiki_retain_delete_guard();
create trigger usage_record_forget_audit_delete after delete on usage_record
  referencing old table as forget_old for each statement execute function evowiki_forget_audit_rows();
