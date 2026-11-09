do $$
declare
  stale constant text[] := array['H1.rsi_propose', 'H1.rsi_gate', 'H1.rsi_review', 'H1.rsi_reflect'];
  reason constant text := '0099：这一项设置对应的步骤已经没有了（独立的讲法改进已并入统一的自动改进），已移除并存档';
  r record;
  v integer;
  n integer;
begin
  insert into harness_history_archive (source_table, user_id, key, row, reason, migrated_at)
  select 'harness_version', h.user_id, h.key, to_jsonb(h), reason, now()
    from harness_version h
   where h.key = any(stale) and h.why <> reason
     and not exists (select 1 from harness_history_archive x
                      where x.source_table = 'harness_version' and x.row = to_jsonb(h));
  get diagnostics n = row_count;
  if n > 0 then
    raise notice '0099 harness: % history row(s) of removed settings archived', n;
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
    raise notice '0099 harness: % ledger row(s) of removed settings archived', n;
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
    raise notice '0099 %: harness setting % removed (archived; was %)', r.user_id, r.key,
      left(r.value #>> '{}', 80);
  end loop;
end $$;

-- 2. track B / B2 rows -----------------------------------------------------------------------------

create table if not exists rsi_b_archive (
  id           bigint generated always as identity primary key,
  source_table text not null,
  user_id      text not null,
  row          jsonb not null,
  archived_at  timestamptz not null
);
create index if not exists rsi_b_archive_user on rsi_b_archive (user_id);

create temp table evowiki_0099_b on commit drop as
select patch_id as id from rsi_patch where track = 'B'
union
select change_id from rsi_method_change where kind = 'B2';

do $$
declare
  n integer;
  t text;
begin
  if to_regclass('public.rsi_b_detail') is not null then
    insert into rsi_b_archive (source_table, user_id, row, archived_at)
    select 'rsi_b_detail', d.user_id, to_jsonb(d), now() from rsi_b_detail d;
    insert into rsi_b_archive (source_table, user_id, row, archived_at)
    select 'rsi_b_consumed', c.user_id, to_jsonb(c), now() from rsi_b_consumed c;
    insert into rsi_b_archive (source_table, user_id, row, archived_at)
    select 'rsi_b_round', b.user_id, to_jsonb(b), now() from rsi_b_round b;
  end if;
  insert into rsi_b_archive (source_table, user_id, row, archived_at)
  select 'rsi_patch', p.user_id, to_jsonb(p), now() from rsi_patch p where p.track = 'B';
  insert into rsi_b_archive (source_table, user_id, row, archived_at)
  select 'rsi_memory', m.user_id, to_jsonb(m), now() from rsi_memory m where m.track = 'B';
  insert into rsi_b_archive (source_table, user_id, row, archived_at)
  select 'rsi_policy_version', p.user_id, to_jsonb(p), now()
    from rsi_policy_version p where p.track = 'B';
  insert into rsi_b_archive (source_table, user_id, row, archived_at)
  select 'rsi_outcome_point', o.user_id, to_jsonb(o), now()
    from rsi_outcome_point o where o.change_id in (select id from evowiki_0099_b);
  insert into rsi_b_archive (source_table, user_id, row, archived_at)
  select 'rsi_method_version', m.user_id, to_jsonb(m), now()
    from rsi_method_version m where m.kind = 'B2';
  insert into rsi_b_archive (source_table, user_id, row, archived_at)
  select 'rsi_method_change', m.user_id, to_jsonb(m), now()
    from rsi_method_change m where m.kind = 'B2';
  insert into rsi_b_archive (source_table, user_id, row, archived_at)
  select 'agent_action', a.user_id, to_jsonb(a), now()
    from agent_action a
   where a.kind in ('rsi_patch', 'rsi_method')
     and exists (select 1 from evowiki_0099_b b where a.content::text like '%"' || b.id || '"%')
     and not exists (select 1 from rsi_b_archive x
                      where x.source_table = 'agent_action'
                        and x.row ->> 'action_id' = a.action_id);
  for t, n in
    select source_table, count(*) from rsi_b_archive
     where archived_at = now() group by source_table order by source_table
  loop
    raise notice '0099 track B: % row(s) of % archived', n, t;
  end loop;
end $$;

alter table rsi_policy_version disable trigger rsi_policy_version_append_only;
alter table rsi_outcome_point disable trigger rsi_outcome_point_append_only;
alter table rsi_method_version disable trigger rsi_method_version_append_only;
delete from rsi_outcome_point where change_id in (select id from evowiki_0099_b);
delete from rsi_policy_version where track = 'B';
delete from rsi_method_version where kind = 'B2';
alter table rsi_method_version enable trigger rsi_method_version_append_only;
alter table rsi_outcome_point enable trigger rsi_outcome_point_append_only;
alter table rsi_policy_version enable trigger rsi_policy_version_append_only;
delete from rsi_method_change where kind = 'B2';
drop table if exists rsi_b_detail;          -- references rsi_patch: dropped before its rows go
drop table if exists rsi_b_consumed;
drop table if exists rsi_b_round;
delete from rsi_memory where track = 'B';
delete from rsi_patch where track = 'B';

alter table rsi_method_version drop constraint if exists rsi_method_version_kind_check;
alter table rsi_method_version add constraint rsi_method_version_kind_check check (kind = 'A2');
alter table rsi_method_change drop constraint if exists rsi_method_change_kind_check;
alter table rsi_method_change add constraint rsi_method_change_kind_check check (kind = 'A2');

-- 3. settings that only steered track B / B2 -------------------------------------------------------

do $$
declare
  r record;
begin
  for r in select user_id, settings from rsi_settings
            where settings ?| array['gate_limit_usd', 'strict_gate', 'layer2'] order by user_id
  loop
    raise notice '0099 %: track-B settings removed (gate_limit_usd %, strict_gate %, layer2 %)',
      r.user_id, coalesce(r.settings ->> 'gate_limit_usd', '-'),
      coalesce(r.settings ->> 'strict_gate', '-'), coalesce(r.settings ->> 'layer2', '-');
  end loop;
end $$;
update rsi_settings
   set settings = settings - 'gate_limit_usd' - 'strict_gate' - 'layer2'
 where settings ?| array['gate_limit_usd', 'strict_gate', 'layer2'];

-- 4. provenance audit (report only) ----------------------------------------------------------------

do $$
declare
  r record;
  n integer := 0;
begin
  for r in
    with latest as (
      select distinct on (e.user_id) e.user_id, e.payload -> 'spec' -> 'items' as items
        from wiki_event e
       where e.type = 'teaching_spec_saved'
       order by e.user_id, e.seq desc)
    select l.user_id, it ->> 'item_id' as item_id, it ->> 'authority' as authority,
           it ->> 'origin' as origin, it ->> 'origin_ref' as origin_ref,
           left(it ->> 'text', 60) as text
      from latest l, jsonb_array_elements(l.items) it
     where it ->> 'origin' = 'rsi_b'
        or (it ->> 'authority' = 'user'
            and exists (select 1 from rsi_b_archive x
                         where x.source_table = 'rsi_patch' and x.user_id = l.user_id
                           and x.row ->> 'patch_id' = it ->> 'origin_ref'))
     order by 1, 2
  loop
    n := n + 1;
    if r.origin = 'rsi_b' then
      raise notice '0099 audit %: % added by track B stays RSI''s (authority %): %',
        r.user_id, r.item_id, r.authority, r.text;
    else
      raise notice '0099 audit %: % is the user''s rule but came from track-B patch % — to check: %',
        r.user_id, r.item_id, r.origin_ref, r.text;
    end if;
  end loop;
  raise notice '0099 audit: % rule(s) from track B in the current teaching specs', n;
end $$;
