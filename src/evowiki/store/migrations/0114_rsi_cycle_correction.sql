alter table rsi_cycle drop constraint rsi_cycle_trigger_check;
alter table rsi_cycle add constraint rsi_cycle_trigger_check
  check (trigger in ('manual', 'every_5_feedback', 'every_10_feedback', 'weekly', 'bot', 'correction'));
