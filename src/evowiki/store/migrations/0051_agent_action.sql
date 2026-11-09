create table agent_action (
  action_id       text primary key,
  user_id         text not null references app_user (user_id),
  kind            text not null,               -- 'assistant_edit', 'curator_structure', 'rsi_patch', 'rsi_method', ...
  scope           text not null check (scope in ('user_notes', 'page_body', 'new_page', 'delete_merge',
                                                 'structure', 'teaching_params', 'rsi', 'suggestion',
                                                 'preference')),
  level           text not null check (level in ('ask', 'auto_undo', 'auto')),
  status          text not null check (status in ('proposed', 'approved', 'rejected', 'executed',
                                                  'failed', 'undone')),
  source          text not null check (source in ('assistant', 'curator', 'rsi', 'bot')),
  summary         text not null,
  why             text not null default '',
  content         jsonb not null,             -- what it changes (diff, draft, ids)
  content_hash    text not null,
  idempotency_key text not null unique,
  page_id         text,
  page_version    text,                       -- page_fingerprint at proposal time (null: no check)
  result          jsonb,
  wiki_event_ids  jsonb not null default '[]'::jsonb,
  undo            jsonb,
  detail          text not null default '',
  run_id          text,
  step            text,
  trigger_id      text,
  created_at      timestamptz not null,
  decided_at      timestamptz,
  expires_at      timestamptz                 -- unused: a pending item never expires
);
create index agent_action_user_created on agent_action (user_id, created_at desc);
create index agent_action_pending on agent_action (user_id) where status = 'proposed';
