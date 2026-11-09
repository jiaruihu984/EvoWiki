create table effect_applied (
    key          text primary key,
    payload_hash text not null,
    result       jsonb not null,
    applied_at   timestamptz not null default now()
);
