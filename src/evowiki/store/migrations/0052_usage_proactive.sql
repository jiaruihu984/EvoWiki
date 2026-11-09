alter table usage_record drop constraint usage_record_grp_check;
alter table usage_record add constraint usage_record_grp_check
  check (grp in ('page', 'assistant', 'curator', 'diagnostic', 'review', 'other', 'proactive'));
