alter table aspect_event drop constraint aspect_event_type_check;
alter table aspect_event add constraint aspect_event_type_check
  check (type in ('define_concept', 'observe', 'revoke_inference', 'declare', 'add_note',
                  'retire_note', 'claim', 'resolve_claim', 'page_read'));
