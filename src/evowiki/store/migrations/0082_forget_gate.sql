do $$
begin
  if not exists (select from pg_roles where rolname = 'evowiki_forget_owner') then
    create role evowiki_forget_owner nologin;
  end if;
  if not exists (select from pg_roles where rolname = 'evowiki_app') then
    create role evowiki_app login;
  end if;
end $$;
alter role evowiki_forget_owner nologin nosuperuser nocreatedb nocreaterole noreplication nobypassrls;
alter role evowiki_app login nosuperuser nocreatedb nocreaterole noreplication nobypassrls;

do $$
begin
  if pg_has_role('evowiki_app', 'evowiki_forget_owner', 'MEMBER') then
    raise exception 'evowiki_app must not be a member of evowiki_forget_owner';
  end if;
  execute format('grant connect, temporary on database %I to evowiki_app', current_database());
  -- tables later migrations create get the same grants (the migrating role creates them)
  execute format('alter default privileges for role %I in schema public '
                 'grant select, insert, update, delete on tables to evowiki_app', current_user);
  execute format('alter default privileges for role %I in schema public '
                 'grant usage, select on sequences to evowiki_app', current_user);
end $$;

grant usage on schema public to evowiki_app, evowiki_forget_owner;
grant select, insert, update, delete on all tables in schema public to evowiki_app;
grant usage, select on all sequences in schema public to evowiki_app;
revoke insert, update, delete on schema_migrations from evowiki_app;

-- DBOS keeps its system tables in schema dbos of this database and migrates them at launch, as the
-- role the service connects as: that role owns the schema and everything in it.
do $$
declare r record;
begin
  if not exists (select from pg_namespace where nspname = 'dbos') then
    create schema dbos authorization evowiki_app;
    return;
  end if;
  alter schema dbos owner to evowiki_app;
  for r in select c.oid::regclass::text as name, c.relkind from pg_class c
            where c.relnamespace = 'dbos'::regnamespace and c.relkind in ('r', 'p', 'v', 'm', 'S')
              and not exists (select from pg_depend d where d.classid = 'pg_class'::regclass
                                and d.objid = c.oid and d.deptype in ('a', 'i')) loop
    execute format('alter %s %s owner to evowiki_app',
                   case r.relkind when 'S' then 'sequence' when 'v' then 'view'
                                  when 'm' then 'materialized view' else 'table' end, r.name);
  end loop;
  for r in select p.oid::regprocedure::text as name from pg_proc p
            where p.pronamespace = 'dbos'::regnamespace loop
    execute format('alter routine %s owner to evowiki_app', r.name);
  end loop;
  for r in select t.oid::regtype::text as name from pg_type t
            where t.typnamespace = 'dbos'::regnamespace and t.typtype in ('e', 'd')
               or (t.typnamespace = 'dbos'::regnamespace and t.typtype = 'c'
                   and (select relkind from pg_class where oid = t.typrelid) = 'c') loop
    execute format('alter type %s owner to evowiki_app', r.name);
  end loop;
end $$;

-- ---------- requests and audit ----------
create table forget_request (
  request_id   text primary key,
  user_id      text not null,
  session_id   text,
  item_id      text,
  state        text not null check (state in ('requested', 'deleting', 'committed', 'failed')),
  requested_at timestamptz not null,
  requested_by text not null,
  updated_at   timestamptz not null,
  error        text,
  opened_xid   xid8,             -- the transaction evowiki_forget_open ran in (the gate's only one)
  check (session_id is not null or item_id is not null)
);
create index forget_request_open on forget_request (user_id) where state <> 'committed';

create table forget_audit (
  id         bigserial primary key,
  request_id text not null references forget_request (request_id),
  at         timestamptz not null default clock_timestamp(),
  step       text not null,
  rows       integer not null
);
create index forget_audit_request on forget_audit (request_id, id);

alter table forget_request owner to evowiki_forget_owner;
alter table forget_audit owner to evowiki_forget_owner;
revoke all on forget_request, forget_audit from public, evowiki_app;
grant select on forget_request, forget_audit to evowiki_app;
revoke all on sequence forget_audit_id_seq from public, evowiki_app;
grant select on app_user, assistant_session to evowiki_forget_owner;

-- ---------- the gate (SECURITY DEFINER, owned by evowiki_forget_owner) ----------
-- No SET clause on these functions: it would undo evowiki_forget_open's set_config at function exit.
-- Every name is schema-qualified instead (pg_catalog is searched first; nothing can shadow it).

create function evowiki_forgetting() returns boolean
language plpgsql security definer as $$
declare rid text := pg_catalog.current_setting('evowiki.forget', true);
begin
  if rid is null or rid = '' then
    return false;
  end if;
  return exists (select from public.forget_request r
                  where r.request_id = rid and r.state = 'deleting'
                    and r.opened_xid = pg_catalog.pg_current_xact_id());
end $$;

create function evowiki_forget_request(p_user_id text, p_session_id text, p_item_id text,
                                       p_requested_by text) returns text
language plpgsql security definer as $$
declare rid text := pg_catalog.gen_random_uuid()::text;
begin
  if p_user_id is null or p_requested_by is null or p_requested_by = ''
     or (p_session_id is null and p_item_id is null) then
    raise exception 'forget request needs a user, a session or an item, and who requested it';
  end if;
  if not exists (select from public.app_user u where u.user_id = p_user_id) then
    raise exception 'forget request: unknown user %', p_user_id;
  end if;
  if p_session_id is not null and not exists (
       select from public.assistant_session s
        where s.session_id = p_session_id and s.user_id = p_user_id) then
    raise exception 'forget request: session % is not a session of user %', p_session_id, p_user_id;
  end if;
  insert into public.forget_request (request_id, user_id, session_id, item_id, state, requested_at,
                                     requested_by, updated_at)
  values (rid, p_user_id, p_session_id, p_item_id, 'requested', pg_catalog.now(), p_requested_by,
          pg_catalog.now());
  insert into public.forget_audit (request_id, step, rows) values (rid, 'request', 0);
  return rid;
end $$;

create function evowiki_forget_open(p_request_id text) returns void
language plpgsql security definer as $$
declare st text;
begin
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

create function evowiki_forget_close(p_request_id text) returns void
language plpgsql security definer as $$
begin
  if pg_catalog.current_setting('evowiki.forget', true) is distinct from p_request_id
     or not public.evowiki_forgetting() then
    raise exception 'forget request % is not open in this transaction', p_request_id;
  end if;
  update public.forget_request set state = 'committed', updated_at = pg_catalog.now()
   where request_id = p_request_id;
  insert into public.forget_audit (request_id, step, rows) values (p_request_id, 'close', 0);
  perform pg_catalog.set_config('evowiki.forget', '', true);
end $$;

-- After a rolled-back forget transaction (the request is back in 'requested' / 'failed'), or for a
-- 'deleting' row left by a transaction that committed without closing: mark it failed for a retry.
create function evowiki_forget_fail(p_request_id text, p_error text) returns void
language plpgsql security definer as $$
declare st text;
begin
  select r.state into st from public.forget_request r where r.request_id = p_request_id for update;
  if st is null then
    raise exception 'forget request % does not exist', p_request_id;
  end if;
  if st = 'committed' then
    raise exception 'forget request % is already committed', p_request_id;
  end if;
  if st = 'deleting' and public.evowiki_forgetting() then
    raise exception 'forget request % is open in this transaction; roll it back first', p_request_id;
  end if;
  update public.forget_request set state = 'failed', error = p_error, updated_at = pg_catalog.now()
   where request_id = p_request_id;
  insert into public.forget_audit (request_id, step, rows) values (p_request_id, 'fail', 0);
end $$;

-- A step of the forget flow on a table without audit triggers (memory_*, S3's evidence): audited by
-- name with its row count, only while the gate is open for that request.
create function evowiki_forget_note(p_request_id text, p_step text, p_rows integer) returns void
language plpgsql security definer as $$
begin
  if pg_catalog.current_setting('evowiki.forget', true) is distinct from p_request_id
     or not public.evowiki_forgetting() then
    raise exception 'forget request % is not open in this transaction', p_request_id;
  end if;
  insert into public.forget_audit (request_id, step, rows) values (p_request_id, p_step, p_rows);
end $$;

-- statement trigger: one audit row per DELETE / UPDATE statement that touched rows with the gate open
create function evowiki_forget_audit_rows() returns trigger
language plpgsql security definer as $$
declare n integer;
begin
  if coalesce(pg_catalog.current_setting('evowiki.forget', true), '') = ''
     or not public.evowiki_forgetting() then
    return null;
  end if;
  if TG_OP = 'DELETE' then
    select pg_catalog.count(*) into n from forget_old;
  else
    select pg_catalog.count(*) into n from forget_new;
  end if;
  if n > 0 then
    insert into public.forget_audit (request_id, step, rows)
    values (pg_catalog.current_setting('evowiki.forget', true),
            pg_catalog.lower(TG_OP) || ':' || TG_TABLE_NAME, n);
  end if;
  return null;
end $$;

do $$
declare f text;
begin
  foreach f in array array['evowiki_forgetting()', 'evowiki_forget_request(text, text, text, text)',
                           'evowiki_forget_open(text)', 'evowiki_forget_close(text)',
                           'evowiki_forget_fail(text, text)', 'evowiki_forget_note(text, text, integer)',
                           'evowiki_forget_audit_rows()'] loop
    execute format('alter function %s owner to evowiki_forget_owner', f);
    execute format('revoke all on function %s from public', f);
    execute format('grant execute on function %s to evowiki_app', f);
  end loop;
end $$;

-- ---------- the guarded tables ----------
-- Row guard of the tables that were plain append-only: closed = evowiki_forbid_mutation's refusal.
-- Trigger arguments = the redaction columns an UPDATE may change while the gate is open.
create function evowiki_forget_guard() returns trigger
language plpgsql as $$
begin
  if public.evowiki_forgetting() then
    if TG_OP = 'DELETE' then
      return old;
    end if;
    if TG_OP = 'UPDATE' and TG_NARGS > 0
       and (to_jsonb(old) - TG_ARGV::text[]) = (to_jsonb(new) - TG_ARGV::text[]) then
      return new;
    end if;
  end if;
  raise exception 'append-only table %: % is forbidden', TG_TABLE_NAME, TG_OP;
end $$;

create or replace function harness_round_guard() returns trigger
language plpgsql as $$
begin
  if public.evowiki_forgetting()
     and (to_jsonb(old) - array['reason', 'finding', 'signal_ids', 'cases', 'candidates', 'comparisons',
                                'decision', 'diff'])
       = (to_jsonb(new) - array['reason', 'finding', 'signal_ids', 'cases', 'candidates', 'comparisons',
                                'decision', 'diff']) then
    return new;
  end if;
  if old.status <> 'running' or new.status not in ('applied', 'asked', 'thin', 'failed', 'no_change',
                                                 'skipped', 'error') then
    raise exception 'harness_round %: status % -> % is forbidden', old.round_id, old.status, new.status;
  end if;
  if new.finished_at is null
     or (new.round_id, new.user_id, new.started_at, new.finding)
        is distinct from (old.round_id, old.user_id, old.started_at, old.finding) then
    raise exception 'harness_round %: only the result columns may be set when it finishes', old.round_id;
  end if;
  return new;
end $$;

create or replace function harness_change_guard() returns trigger
language plpgsql as $$
begin
  if public.evowiki_forgetting()
     and (to_jsonb(old) - array['reason', 'diff']) = (to_jsonb(new) - array['reason', 'diff']) then
    return new;
  end if;
  if not ((old.status = 'asked' and new.status in ('applied', 'rejected'))
          or (old.status = 'applied' and new.status in ('undone', 'rolled_back'))) then
    raise exception 'harness_change %: status % -> % is forbidden', old.change_id, old.status, new.status;
  end if;
  if new.settled_at is null
     or (new.change_id, new.user_id, new.round_id, new.diff, new.cost_before, new.cost_after,
         new.reason, new.created_at)
        is distinct from (old.change_id, old.user_id, old.round_id, old.diff, old.cost_before,
                          old.cost_after, old.reason, old.created_at)
     or (new.version is distinct from old.version
         and not (old.status = 'asked' and new.status = 'applied')) then
    raise exception 'harness_change %: only status, settled_at and (on apply) version may change',
      old.change_id;
  end if;
  return new;
end $$;

drop trigger assistant_session_append_only on assistant_session;
create trigger assistant_session_append_only before update or delete on assistant_session
  for each row execute function evowiki_forget_guard();
drop trigger assistant_entry_append_only on assistant_entry;
create trigger assistant_entry_append_only before update or delete on assistant_entry
  for each row execute function evowiki_forget_guard('payload');
drop trigger wiki_event_append_only on wiki_event;
create trigger wiki_event_append_only before update or delete on wiki_event
  for each row execute function evowiki_forget_guard('payload');
drop trigger llm_call_append_only on llm_call;
create trigger llm_call_append_only before update or delete on llm_call
  for each row execute function evowiki_forget_guard();
drop trigger llm_attempt_append_only on llm_attempt;
create trigger llm_attempt_append_only before update or delete on llm_attempt
  for each row execute function evowiki_forget_guard();
drop trigger harness_experience_append_only on harness_experience;
create trigger harness_experience_append_only before update or delete on harness_experience
  for each row execute function evowiki_forget_guard('situation', 'change', 'evidence');
drop trigger harness_round_no_delete on harness_round;
create trigger harness_round_no_delete before delete on harness_round
  for each row execute function evowiki_forget_guard();
drop trigger harness_change_no_delete on harness_change;
create trigger harness_change_no_delete before delete on harness_change
  for each row execute function evowiki_forget_guard();
drop trigger harness_version_append_only on harness_version;
create trigger harness_version_append_only before update on harness_version
  for each row execute function evowiki_forget_guard('before', 'after', 'why');
create trigger harness_version_no_delete before delete on harness_version
  for each row execute function evowiki_forbid_mutation();
create trigger harness_version_forget_audit_update after update on harness_version
  referencing new table as forget_new for each statement execute function evowiki_forget_audit_rows();

do $$
declare t text;
begin
  foreach t in array array['assistant_session', 'assistant_entry', 'wiki_event', 'harness_round',
                           'harness_change', 'harness_experience', 'llm_call', 'llm_request', 'llm_text',
                           'llm_attempt', 'agent_run', 'agent_step', 'agent_action'] loop
    execute format('create trigger %I after delete on %I referencing old table as forget_old '
                   'for each statement execute function evowiki_forget_audit_rows()',
                   t || '_forget_audit_delete', t);
    execute format('create trigger %I after update on %I referencing new table as forget_new '
                   'for each statement execute function evowiki_forget_audit_rows()',
                   t || '_forget_audit_update', t);
  end loop;
end $$;
