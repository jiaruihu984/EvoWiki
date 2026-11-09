create table component_library (
  component_id  text primary key check (component_id ~ '^[a-z0-9][a-z0-9-]{1,63}$'),
  body          jsonb not null check ((body -> 'check' ->> 'ok')::boolean),
  registered_at timestamptz not null default clock_timestamp()
);
