alter table usage_record
  add column cache_read_tokens  bigint not null default 0 check (cache_read_tokens >= 0),
  add column cache_write_tokens bigint not null default 0 check (cache_write_tokens >= 0);
