alter table llm_call add column refused_at timestamptz;

alter table llm_call drop constraint llm_call_execution_id_distinct;
alter table llm_call add constraint llm_call_execution_id_distinct
  check (replayable or text_expired_at is not null or refused_at is not null
         or id <> 'llm_' || left(input_hash, 20));

-- The row guard stays for everything else: only this one change (replayable -> false, refused_at set,
-- nothing else) passes it.
drop trigger llm_call_append_only on llm_call;
create trigger llm_call_append_only before delete on llm_call
  for each row execute function evowiki_retain_guard('raw_output', 'parsed_output', 'replayable',
                                                     'text_expired_at');
create trigger llm_call_append_only_update before update on llm_call
  for each row when (not (old.replayable and not new.replayable
                          and old.refused_at is null and new.refused_at is not null
                          and (to_jsonb(old) - 'replayable' - 'refused_at')
                              = (to_jsonb(new) - 'replayable' - 'refused_at')))
  execute function evowiki_retain_guard('raw_output', 'parsed_output', 'replayable', 'text_expired_at');
