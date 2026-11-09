alter table wiki_event disable trigger wiki_event_append_only;
update wiki_event set payload = payload #- '{record,refined}'
  where type = 'page_saved' and payload->'record' ? 'refined';
update wiki_event set payload = (payload #- '{after,refined}') #- '{before,refined}'
  where type = 'feedback_applied' and (payload->'after' ? 'refined' or payload->'before' ? 'refined');
alter table wiki_event enable trigger wiki_event_append_only;
update agent_action set undo = undo #- '{prev_record,refined}'
  where undo->'prev_record' ? 'refined';
