alter table agent_action drop constraint agent_action_source_check;
alter table agent_action add constraint agent_action_source_check
  check (source in ('assistant', 'curator', 'rsi', 'bot', 'user'));

alter table page_run drop constraint page_run_kind_check;
alter table page_run add constraint page_run_kind_check
    check (kind in ('organize','prefetch','feedback','refine','rsi_rewrite','reorder_rewrite',
                    'source_revise'));
