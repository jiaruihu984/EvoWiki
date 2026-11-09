create function pg_temp.evowiki_0080_strip(j jsonb) returns jsonb language plpgsql immutable as $f$
begin
  if jsonb_typeof(j) = 'object' then
    return coalesce((select jsonb_object_agg(e.key, pg_temp.evowiki_0080_strip(e.value))
                       from jsonb_each(j) as e where e.key <> 'background'), '{}'::jsonb);
  elsif jsonb_typeof(j) = 'array' then
    return coalesce((select jsonb_agg(pg_temp.evowiki_0080_strip(a.v) order by a.i)
                       from jsonb_array_elements(j) with ordinality as a(v, i)), '[]'::jsonb);
  end if;
  return j;
end $f$;

-- profile/note._norm: Python's str.split() whitespace, collapsed to one space, trimmed
create function pg_temp.evowiki_0080_norm(t text) returns text language sql immutable as $f$
  select btrim(regexp_replace(t, '[[:space:]\u0085   - 

  　\u001c-\u001f]+',
                              ' ', 'g'), ' ')
$f$;

-- 1. copy -------------------------------------------------------------------------------------
do $$
declare
  r record;
  pbody jsonb;
  who text[];
  moved text[];
  skipped text[];
begin
  for r in
    with stored as (
      select e.user_id, e.seq, v #>> '{}' as b
        from wiki_event e,
             jsonb_path_query(e.payload, 'strict $.**.background', '{}', true) as v
       where jsonb_typeof(v) = 'string'),
    lines as (
      select s.user_id, s.seq, l.o, pg_temp.evowiki_0080_norm(l.line) as line
        from stored s, regexp_split_to_table(s.b, '\r\n|\r|\n') with ordinality as l(line, o)),
    firsts as (
      select user_id, line, min(array[seq, o]) as first
        from lines where line <> '' group by user_id, line)
    select user_id, array_agg(line order by first, line) as lines from firsts group by user_id
    order by user_id
  loop
    skipped := array(select u.l from unnest(r.lines) with ordinality as u(l, o)
                      where u.l like '他说：%' order by u.o);
    select p.body into pbody from learner_profile p where p.user_id = r.user_id for update;
    pbody := coalesce(pbody, '{}'::jsonb);
    who := array(select jsonb_array_elements_text(coalesce(pbody -> 'owned' -> '我是谁', '[]'::jsonb)));
    moved := array(select u.l from unnest(r.lines) with ordinality as u(l, o)
                    where not (u.l = any(who)) and not (u.l = any(skipped))
                      and not (u.l = any(array(select pg_temp.evowiki_0080_norm(x) from unnest(who) x)))
                    order by u.o);
    if coalesce(array_length(moved, 1), 0) > 0 then
      pbody := jsonb_set(pbody, '{owned}', coalesce(pbody -> 'owned', '{}'::jsonb), true);
      pbody := jsonb_set(pbody, '{owned,我是谁}', to_jsonb(who || moved), true);
      insert into learner_profile (user_id, body, updated_at) values (r.user_id, pbody, now())
        on conflict (user_id) do update set body = excluded.body, updated_at = excluded.updated_at;
      raise notice '0080 %: moved to 我是谁: %', r.user_id, array_to_string(moved, ' | ');
    else
      raise notice '0080 %: nothing new for 我是谁 (already there)', r.user_id;
    end if;
    if coalesce(array_length(skipped, 1), 0) > 0 then
      raise notice '0080 %: not moved (written by code, not his words): %', r.user_id,
        array_to_string(skipped, ' | ');
    end if;
  end loop;
end $$;

-- 2. stale harness settings ---------------------------------------------------------------------
do $$
begin    -- created once (a re-run of this file finds them: no "already exists" notice in the report)
  if to_regclass('harness_value_archive') is null then
    create table harness_value_archive (
      user_id     text not null,
      key         text not null,
      value       jsonb not null,
      source      text not null,
      version     integer not null,
      updated_at  timestamptz not null,
      reason      text not null,
      migrated_at timestamptz not null
    );
  end if;
  if to_regclass('harness_history_archive') is null then
    create table harness_history_archive (
      source_table text not null,               -- 'harness_version' | 'agent_action'
      user_id      text not null,
      key          text not null,
      row          jsonb not null,              -- the row as it was (to_jsonb)
      reason       text not null,
      migrated_at  timestamptz not null
    );
  end if;
end $$;

do $$
declare
  stale constant text[] := array['H1.refine', 'H1.source_split', 'H2.assistant.new_goal.background'];
  reason constant text := '0080：这一项设置对应的步骤已经没有了（S1 定目标一次调用），已移除并存档';
  r record;
  v integer;
  n integer;
begin
  insert into harness_history_archive (source_table, user_id, key, row, reason, migrated_at)
  select 'harness_version', h.user_id, h.key, to_jsonb(h), reason, now()
    from harness_version h
   where h.key = any(stale) and h.why <> reason          -- not 0080's own removal rows
     and not exists (select 1 from harness_history_archive x
                      where x.source_table = 'harness_version' and x.row = to_jsonb(h));
  get diagnostics n = row_count;
  if n > 0 then
    raise notice '0080 harness: % history row(s) of removed settings archived', n;
  end if;
  insert into harness_history_archive (source_table, user_id, key, row, reason, migrated_at)
  select 'agent_action', a.user_id, 'H1.' || (a.content ->> 'step'), to_jsonb(a), reason, now()
    from agent_action a
   where a.kind = 'prompt_override' and ('H1.' || (a.content ->> 'step')) = any(stale)
     and not exists (select 1 from harness_history_archive x
                      where x.source_table = 'agent_action'
                        and x.row ->> 'action_id' = a.action_id);
  get diagnostics n = row_count;
  if n > 0 then
    raise notice '0080 harness: % ledger row(s) of removed settings archived', n;
  end if;
  for r in select * from harness_value where key = any(stale) order by user_id, key for update
  loop
    insert into harness_value_archive (user_id, key, value, source, version, updated_at, reason,
                                       migrated_at)
    values (r.user_id, r.key, r.value, r.source, r.version, r.updated_at, reason, now());
    select coalesce(max(version), 0) + 1 into v from harness_version where user_id = r.user_id;
    insert into harness_version (user_id, version, key, before, after, source, why, created_at)
    values (r.user_id, v, r.key, r.value, null, 'rollback', reason, now());
    delete from harness_value where user_id = r.user_id and key = r.key;
    raise notice '0080 %: harness setting % removed (archived; was %)', r.user_id, r.key,
      left(r.value #>> '{}', 80);
  end loop;
end $$;

-- 3. delete ----------------------------------------------------------------------------------
alter table wiki_event disable trigger wiki_event_append_only;
update wiki_event set payload = pg_temp.evowiki_0080_strip(payload)
 where jsonb_path_exists(payload, 'strict $.**.background', '{}', true);
alter table wiki_event enable trigger wiki_event_append_only;

update agent_action set content = pg_temp.evowiki_0080_strip(content)
 where jsonb_path_exists(content, 'strict $.**.background', '{}', true);
update agent_action set result = pg_temp.evowiki_0080_strip(result)
 where result is not null and jsonb_path_exists(result, 'strict $.**.background', '{}', true);
update agent_action set undo = pg_temp.evowiki_0080_strip(undo)
 where undo is not null and jsonb_path_exists(undo, 'strict $.**.background', '{}', true);

update page_run set job = pg_temp.evowiki_0080_strip(job)
 where jsonb_path_exists(job, 'strict $.**.background', '{}', true);
update page_run set held = pg_temp.evowiki_0080_strip(held)
 where held is not null and jsonb_path_exists(held, 'strict $.**.background', '{}', true);

delete from wiki_snapshot;

drop function pg_temp.evowiki_0080_strip(jsonb);
drop function pg_temp.evowiki_0080_norm(text);
