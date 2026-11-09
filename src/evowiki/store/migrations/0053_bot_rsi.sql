alter table rsi_cycle drop constraint rsi_cycle_trigger_check;
alter table rsi_cycle add constraint rsi_cycle_trigger_check
  check (trigger in ('manual', 'every_5_feedback', 'weekly', 'bot'));

alter table bot_trigger drop constraint bot_trigger_status_check;
alter table bot_trigger add constraint bot_trigger_status_check
  check (status in ('new', 'gated', 'awaiting_plan', 'planned', 'handed_off', 'failed',
                    'awaiting_rsi'));

alter table bot_kind_pause drop constraint bot_kind_pause_kind_check;
alter table bot_kind_pause add constraint bot_kind_pause_kind_check
  check (kind in ('plan', 'check', 'daily', 'dispatch', 'rsi'));
