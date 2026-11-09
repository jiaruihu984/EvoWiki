alter table usage_settings add column models jsonb not null default '{}'::jsonb;

-- The picker reads each provider's latest attempt (a key rejected for balance / access shows the model disabled).
create index llm_attempt_provider_ts on llm_attempt (provider, ts desc);
