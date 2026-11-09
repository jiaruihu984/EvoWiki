alter table assistant_entry drop constraint if exists assistant_entry_type_check;
alter table assistant_entry add constraint assistant_entry_type_check
  check (type in ('user', 'tool', 'assistant', 'proposal', 'decision', 'observed', 'compaction',
                  'turn_error', 'rating'));
