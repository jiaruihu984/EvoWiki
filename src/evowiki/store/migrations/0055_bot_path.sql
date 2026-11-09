alter table agent_action drop constraint agent_action_scope_check;
alter table agent_action add constraint agent_action_scope_check
  check (scope in ('user_notes', 'page_body', 'new_page', 'delete_merge', 'structure',
                   'teaching_params', 'rsi', 'suggestion', 'preference', 'path'));
