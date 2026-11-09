alter table wiki_event disable trigger wiki_event_append_only;
update wiki_event e set payload = jsonb_set(e.payload, '{record,nodes}', coalesce((
    select jsonb_agg(
             case when n->>'state' = 'known' then jsonb_set(n - 'known_why', '{state}', '"collapsed"')
                  else n - 'known_why' end
             order by i)
      from jsonb_array_elements(e.payload->'record'->'nodes') with ordinality as x(n, i)), '[]'::jsonb))
  where e.type = 'goal_map_saved'
    and exists (select 1 from jsonb_array_elements(e.payload->'record'->'nodes') n
                 where n ? 'known_why' or n->>'state' = 'known');
update wiki_event set payload = jsonb_set(payload, '{action}', '"diagnosed"')
  where type = 'goal_map_saved' and payload->>'action' = 'known';
alter table wiki_event enable trigger wiki_event_append_only;
delete from wiki_snapshot;
