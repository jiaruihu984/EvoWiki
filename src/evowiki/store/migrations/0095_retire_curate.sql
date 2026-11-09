create table if not exists migration_archive (
  migration    text not null,
  source_table text not null,
  user_id      text,
  row          jsonb not null,       -- the row as it was (to_jsonb)
  reason       text not null,
  migrated_at  timestamptz not null
);

-- ---- 1. H1.curate ----------------------------------------------------------------------------
do $$
declare
  stale constant text[] := array['H1.curate'];
  reason constant text := '0095：这一项设置对应的步骤「Wiki 内容整理」已经没有了（整理 Wiki 已废弃，只保留知识图谱分组起名），已移除并存档';
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
    raise notice '0095 harness: % history row(s) of removed settings archived', n;
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
    raise notice '0095 harness: % ledger row(s) of removed settings archived', n;
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
    raise notice '0095 %: harness setting % removed (archived; was %)', r.user_id, r.key,
      left(r.value #>> '{}', 80);
  end loop;
end $$;

-- ---- 2. hubs ---------------------------------------------------------------------------------
create temp table hub_0095 (
  user_id    text not null,
  hub_id     text not null,
  referenced boolean not null default false,
  primary key (user_id, hub_id)
) on commit drop;

insert into hub_0095 (user_id, hub_id)
select distinct e.user_id, h ->> 'id'
  from wiki_event e, jsonb_array_elements(coalesce(e.payload #> '{ledger,structure,hubs}', '[]')) h
 where e.type = 'ledger_saved' and h ->> 'id' is not null
union
select distinct e.user_id, x ->> 'hub_id'
  from wiki_event e, jsonb_array_elements(coalesce(e.payload #> '{ledger,history}', '[]')) hist,
       lateral (select hist -> 'intent' as x) i
 where e.type = 'ledger_saved' and x ->> 'kind' = 'CREATE_HUB' and x ->> 'hub_id' is not null
union
select distinct e.user_id, r ->> 'dst'
  from wiki_event e, jsonb_array_elements(coalesce(e.payload #> '{graph,relations}', '[]')) r
 where e.type = 'graph_saved' and r ->> 'source' = 'hub'
on conflict do nothing;

-- used elsewhere: any wiki event that is not the hub's own record (the curator's ledger, the
-- graph, names, group names, the retired types), or any K event besides its definition
update hub_0095 h set referenced = true
 where exists (select 1 from wiki_event e
                where e.user_id = h.user_id
                  and e.type not in ('ledger_saved', 'graph_saved', 'concepts_named',
                                     'groups_named', 'settings_set', 'moves_proposed',
                                     'moves_decided', 'curate_run_started', 'curate_run_finished')
                  and strpos(e.payload::text, '"' || h.hub_id || '"') > 0)
    or exists (select 1 from aspect_event a
                where a.user_id = h.user_id and a.type <> 'define_concept'
                  and strpos(a.payload::text, h.hub_id) > 0);

-- goal maps' (member, parent) pairs: a kept hub keeps the part_of a goal map also draws
create temp table gm_child_0095 on commit drop as
select distinct e.user_id, n ->> 'id' as child, n ->> 'parent' as parent
  from wiki_event e, jsonb_array_elements(coalesce(e.payload #> '{record,nodes}', '[]')) n
 where e.type = 'goal_map_saved' and n ->> 'parent' is not null;

do $$
declare
  r record;
  dropped text;
begin
  for r in select * from hub_0095 order by user_id, hub_id loop
    if r.referenced then
      select string_agg((x ->> 'src') || '→' || (x ->> 'dst'), '、' order by x ->> 'src') into dropped
        from (select payload from wiki_event
               where user_id = r.user_id and type = 'graph_saved' order by seq desc limit 1) g,
             jsonb_array_elements(coalesce(g.payload #> '{graph,relations}', '[]')) x
       where x ->> 'source' = 'hub' and x ->> 'dst' = r.hub_id
         and not exists (select 1 from gm_child_0095 c where c.user_id = r.user_id
                          and c.child = x ->> 'src' and c.parent = x ->> 'dst');
      raise notice '0095 %: hub % is used elsewhere: kept as an ordinary block; part_of dropped: %',
        r.user_id, r.hub_id, coalesce(dropped, '（无）');
    else
      raise notice '0095 %: hub % is not used elsewhere: deleted (node, edges, names, K)',
        r.user_id, r.hub_id;
    end if;
  end loop;
end $$;

alter table wiki_event disable trigger wiki_event_append_only;
alter table aspect_event disable trigger aspect_event_append_only;

-- the backup of every wiki event changed or deleted below
insert into migration_archive (migration, source_table, user_id, row, reason, migrated_at)
select '0095', 'wiki_event', e.user_id, to_jsonb(e), '整理 Wiki 废弃：改写或删除前的原样', now()
  from wiki_event e
 where e.type in ('ledger_saved', 'settings_set', 'moves_proposed', 'moves_decided',
                  'curate_run_started', 'curate_run_finished')
    or (e.type = 'graph_saved' and (e.payload ->> 'action' = 'hubs_synced'
        or exists (select 1 from hub_0095 h where h.user_id = e.user_id)))
    or (e.type in ('concepts_named', 'groups_named')
        and exists (select 1 from hub_0095 h where h.user_id = e.user_id and not h.referenced
                     and strpos(e.payload::text, '"' || h.hub_id || '"') > 0));

-- the graph: unused hubs out, source-"hub" edges kept only where a goal map draws them
update wiki_event e set payload = jsonb_set(jsonb_set(e.payload, '{graph,relations}', coalesce((
    select jsonb_agg(x.r order by x.i)
      from jsonb_array_elements(e.payload #> '{graph,relations}') with ordinality x(r, i)
     where not exists (select 1 from hub_0095 h where h.user_id = e.user_id and not h.referenced
                        and h.hub_id in (x.r ->> 'src', x.r ->> 'dst'))
       and (x.r ->> 'source' is distinct from 'hub'
            or (exists (select 1 from gm_child_0095 c where c.user_id = e.user_id
                         and c.child = x.r ->> 'src' and c.parent = x.r ->> 'dst')
                and not exists (select 1 from jsonb_array_elements(e.payload #> '{graph,relations}') y
                                 where y ->> 'source' is distinct from 'hub'
                                   and y ->> 'kind' = x.r ->> 'kind'
                                   and y ->> 'src' = x.r ->> 'src'
                                   and y ->> 'dst' = x.r ->> 'dst')))), '[]'::jsonb)),
    '{graph,nodes}', coalesce((
    select jsonb_agg(x.n order by x.i)
      from jsonb_array_elements(e.payload #> '{graph,nodes}') with ordinality x(n, i)
     where not exists (select 1 from hub_0095 h where h.user_id = e.user_id and not h.referenced
                        and h.hub_id = x.n ->> 'id')), '[]'::jsonb))
 where e.type = 'graph_saved' and exists (select 1 from hub_0095 h where h.user_id = e.user_id);
-- what is left of the "hub" source (kept edges, and the before / after copies in the change log)
update wiki_event set payload = replace(payload::text, '"source": "hub"', '"source": "migrated"')::jsonb
 where type = 'graph_saved' and strpos(payload::text, '"source": "hub"') > 0;
update wiki_event set payload = jsonb_set(payload, '{action}', '"migrated"')
 where type = 'graph_saved' and payload ->> 'action' = 'hubs_synced';

-- names and group names of the unused hubs
update wiki_event e set payload = jsonb_set(e.payload, '{names}', coalesce((
    select jsonb_agg(x.n order by x.i)
      from jsonb_array_elements(e.payload -> 'names') with ordinality x(n, i)
     where not exists (select 1 from hub_0095 h where h.user_id = e.user_id and not h.referenced
                        and h.hub_id = x.n ->> 'concept_id')), '[]'::jsonb))
 where e.type = 'concepts_named'
   and exists (select 1 from hub_0095 h where h.user_id = e.user_id and not h.referenced);
update wiki_event e set payload = jsonb_set(e.payload, '{names}', coalesce((
    select jsonb_agg(x.n order by x.i)
      from jsonb_array_elements(e.payload -> 'names') with ordinality x(n, i)
     where not exists (select 1 from hub_0095 h where h.user_id = e.user_id and not h.referenced
                        and x.n -> 'members' ? h.hub_id)), '[]'::jsonb))
 where e.type = 'groups_named'
   and exists (select 1 from hub_0095 h where h.user_id = e.user_id and not h.referenced);
delete from wiki_event where type in ('concepts_named', 'groups_named') and payload -> 'names' = '[]'::jsonb;

-- K: the unused hubs' definitions out, and their ids out of other concepts' prereqs
insert into migration_archive (migration, source_table, user_id, row, reason, migrated_at)
select '0095', 'aspect_event', a.user_id, to_jsonb(a), '整理 Wiki 废弃：没被用到的主题的 K 定义', now()
  from aspect_event a
 where a.type = 'define_concept'
   and exists (select 1 from hub_0095 h where h.user_id = a.user_id and not h.referenced
                and (h.hub_id = a.payload ->> 'concept_id' or a.payload -> 'prereqs' ? h.hub_id));
delete from aspect_event a
 where a.type = 'define_concept'
   and exists (select 1 from hub_0095 h where h.user_id = a.user_id and not h.referenced
                and h.hub_id = a.payload ->> 'concept_id');
update aspect_event a set payload = jsonb_set(a.payload, '{prereqs}', coalesce((
    select jsonb_agg(p order by i)
      from jsonb_array_elements_text(a.payload -> 'prereqs') with ordinality x(p, i)
     where not exists (select 1 from hub_0095 h where h.user_id = a.user_id and not h.referenced
                        and h.hub_id = p)), '[]'::jsonb))
 where a.type = 'define_concept'
   and exists (select 1 from hub_0095 h where h.user_id = a.user_id and not h.referenced
                and a.payload -> 'prereqs' ? h.hub_id);

-- ---- 3. what nothing reads any more ------------------------------------------------------------
delete from wiki_event
 where type in ('ledger_saved', 'settings_set', 'moves_proposed', 'moves_decided',
                'curate_run_started', 'curate_run_finished');

alter table aspect_event enable trigger aspect_event_append_only;
alter table wiki_event enable trigger wiki_event_append_only;

insert into migration_archive (migration, source_table, user_id, row, reason, migrated_at)
select '0095', 'agent_action', a.user_id, to_jsonb(a), '整理 Wiki 废弃：整理的提案和历史', now()
  from agent_action a where a.kind = 'curator_structure';
delete from agent_action where kind = 'curator_structure';

update page_run set job = job - 'curate' where job ? 'curate';

do $$
declare
  n integer;
begin
  if to_regclass('dbos.workflow_status') is not null then
    update dbos.workflow_status set status = 'CANCELLED'
     where name = 'evowiki.curate' and status in ('PENDING', 'ENQUEUED');
    get diagnostics n = row_count;
    if n > 0 then
      raise notice '0095: % pending evowiki.curate workflow(s) cancelled', n;
    end if;
  end if;
end $$;

-- ---- fail closed: nothing may still hold what was removed ------------------------------------
do $$
declare
  r record;
  n integer;
begin
  for r in select * from hub_0095 where not referenced loop
    select count(*) into n from wiki_event
     where user_id = r.user_id and strpos(payload::text, '"' || r.hub_id || '"') > 0;
    if n > 0 then
      raise exception '0095 %: % wiki event(s) still hold the deleted hub %', r.user_id, n, r.hub_id;
    end if;
    select count(*) into n from aspect_event
     where user_id = r.user_id and strpos(payload::text, r.hub_id) > 0;
    if n > 0 then
      raise exception '0095 %: % K event(s) still hold the deleted hub %', r.user_id, n, r.hub_id;
    end if;
  end loop;
  select count(*) into n from wiki_event
   where type = 'graph_saved' and strpos(payload::text, '"source": "hub"') > 0;
  if n > 0 then
    raise exception '0095: % graph(s) still have a "hub" source', n;
  end if;
  select count(*) into n from migration_archive where migration = '0095';
  raise notice '0095: % row(s) archived in migration_archive before they were changed or deleted', n;
end $$;

-- ---- 4. the cached states carry the old shape: the next load replays ---------------------------
delete from wiki_snapshot;
