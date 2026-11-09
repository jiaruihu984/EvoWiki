create function pg_temp.evowiki_0085_default(it jsonb) returns boolean language sql immutable as $f$
  select it ->> 'item_id' ~ '^d[0-9]{2}$' and it ->> 'source' in ('default', 'rsi')
$f$;

create function pg_temp.evowiki_0085_item(it jsonb, kind text) returns jsonb language sql immutable as $f$
  select case
    when it ? 'authority' then it
    else (it - 'source' - 'origin')
         || jsonb_build_object(
              'origin', case when kind = 'import' then 'import'
                             when pg_temp.evowiki_0085_default(it) then 'default'
                             else 'user_ui' end,
              'authority', case when kind <> 'import' and pg_temp.evowiki_0085_default(it)
                                then 'rsi'
                                else 'user' end,
              'origin_ref', case when jsonb_typeof(it -> 'origin') = 'string'
                                 then to_jsonb(left(it ->> 'origin', 200)) else 'null'::jsonb end,
              'rev', 1,
              'rev_by', case when kind = 'import' or not pg_temp.evowiki_0085_default(it) then 'user'
                             when it ->> 'source' = 'rsi' then 'rsi'
                             else 'system' end,
              'protected', kind <> 'import' and pg_temp.evowiki_0085_default(it)
                           and it ->> 'item_id' in ('d12', 'd16', 'd18', 'd20'))
  end
$f$;

create function pg_temp.evowiki_0085_items(items jsonb, kind text) returns jsonb language sql immutable as $f$
  select coalesce(jsonb_agg(pg_temp.evowiki_0085_item(a.v, kind) order by a.i), '[]'::jsonb)
    from jsonb_array_elements(items) with ordinality as a(v, i)
$f$;

create temp table evowiki_0085_old on commit drop as
select e.user_id, e.event_id, e.type,
       case when e.type = 'teaching_spec_saved' then '{spec,items}'::text[]
            else '{preview,items}'::text[] end as path
  from wiki_event e
 where e.type in ('teaching_spec_saved', 'spec_import_previewed')
   and exists (select 1 from jsonb_array_elements(
                 case when e.type = 'teaching_spec_saved' then e.payload -> 'spec' -> 'items'
                      else e.payload -> 'preview' -> 'items' end) it
               where not (it ? 'authority'));

do $$
declare
  r record;
begin
  for r in
    select o.user_id,
           count(*) filter (where o.type = 'teaching_spec_saved'
                                  and pg_temp.evowiki_0085_default(it)) as defaults,
           count(*) filter (where not (o.type = 'teaching_spec_saved'
                                       and pg_temp.evowiki_0085_default(it))) as theirs,
           count(distinct o.event_id) as events
      from evowiki_0085_old o
      join wiki_event e on e.event_id = o.event_id,
           jsonb_array_elements(e.payload #> o.path) it
     where not (it ? 'authority')
     group by o.user_id order by o.user_id
  loop
    raise notice '0085 %: % event(s): % default rule item(s) (RSI''s to tune), % the user''s own',
      r.user_id, r.events, r.defaults, r.theirs;
  end loop;
end $$;

alter table wiki_event disable trigger wiki_event_append_only;
update wiki_event e
   set payload = jsonb_set(e.payload, o.path,
                           pg_temp.evowiki_0085_items(e.payload #> o.path,
                             case when o.type = 'spec_import_previewed' then 'import' else 'spec' end))
  from evowiki_0085_old o
 where e.event_id = o.event_id;
alter table wiki_event enable trigger wiki_event_append_only;

delete from wiki_snapshot where user_id in (select distinct user_id from evowiki_0085_old);

drop function pg_temp.evowiki_0085_items(jsonb, text);
drop function pg_temp.evowiki_0085_item(jsonb, text);
drop function pg_temp.evowiki_0085_default(jsonb);
