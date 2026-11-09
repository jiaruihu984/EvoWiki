alter table rsi_b_round drop constraint rsi_b_round_status_check;
alter table rsi_b_round add constraint rsi_b_round_status_check
  check (status in ('no_patch', 'unusable_output'));
