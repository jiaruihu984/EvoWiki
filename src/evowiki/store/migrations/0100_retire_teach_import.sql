do $$
declare
  stale constant text[] := array['H1.teach_import'];
  reason constant text := '0100：这一项设置对应的步骤已经没有了（贴来的讲解说明改在聊天里记成偏好），已移除并存档';
  r record;
  v integer;
  n integer;
begin
  if exists (select 1 from wiki_event e,
                    jsonb_array_elements(e.payload -> 'spec' -> 'items') it
              where e.type = 'teaching_spec_saved' and it ->> 'origin' = 'import') then
    raise exception '0100: a stored teaching rule still has origin import; settle it before this migration';
  end if;

  insert into harness_history_archive (source_table, user_id, key, row, reason, migrated_at)
  select 'harness_version', h.user_id, h.key, to_jsonb(h), reason, now()
    from harness_version h
   where h.key = any(stale) and h.why <> reason          -- not 0100's own removal rows
     and not exists (select 1 from harness_history_archive x
                      where x.source_table = 'harness_version' and x.row = to_jsonb(h));
  get diagnostics n = row_count;
  if n > 0 then
    raise notice '0100 harness: % history row(s) of removed settings archived', n;
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
    raise notice '0100 harness: % ledger row(s) of removed settings archived', n;
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
    raise notice '0100 %: harness setting % removed (archived; was %)', r.user_id, r.key,
      left(r.value #>> '{}', 80);
  end loop;

  if exists (select 1 from wiki_event
              where type in ('spec_import_previewed', 'spec_import_closed')) then
    delete from wiki_snapshot where user_id in (
      select distinct user_id from wiki_event
       where type in ('spec_import_previewed', 'spec_import_closed'));
    alter table wiki_event disable trigger wiki_event_append_only;
    delete from wiki_event where type in ('spec_import_previewed', 'spec_import_closed');
    get diagnostics n = row_count;
    alter table wiki_event enable trigger wiki_event_append_only;
    raise notice '0100: % import preview event(s) deleted', n;
  end if;
end $$;
