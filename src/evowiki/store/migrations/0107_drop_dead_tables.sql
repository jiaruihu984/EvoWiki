alter table event drop constraint event_render_plan_fk;

do $$
declare
  t text;
  n bigint;
begin
  foreach t in array array['engine_state', 'view_entry', 'lease_record', 'bridge_check',
                           'extraction_output', 'reconcile_report', 'harness_history_archive',
                           'harness_value_archive', 'node_graph_backup']
  loop
    execute format('select count(*) from %I', t) into n;
    if n > 0 then
      raise exception '0107: % has % row(s); settle it before this migration', t, n;
    end if;
    execute format('drop table %I', t);
  end loop;
  foreach t in array array['belief', 'belief_state', 'render_plan', 'user_policy', 'extraction_job',
                           'section_cache', 'migration_archive', 'rsi_b_archive', 'evidence',
                           'event']
  loop
    execute format('select count(*) from %I', t) into n;
    raise notice '0107: dropping % with % row(s)', t, n;
    execute format('drop table %I', t);
  end loop;
end $$;

drop function render_plan_guard();
