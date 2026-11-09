create table memory_conflict (
  conflict_id text primary key,
  user_id     text not null references app_user (user_id),
  item_ids    text[] not null check (cardinality(item_ids) >= 2),   -- sorted, distinct
  why         text not null,
  status      text not null check (status in ('open', 'kept_both', 'resolved')),
  detected_at timestamptz not null,
  resolved_at timestamptz,
  check ((status = 'open') = (resolved_at is null))
);
create index memory_conflict_user on memory_conflict (user_id, status);
create unique index memory_conflict_open_group on memory_conflict (user_id, item_ids)
  where status = 'open';

-- The library a user's last check read (its hash): an unchanged library is not read again.
create table memory_conflict_scan (
  user_id      text primary key references app_user (user_id),
  library_hash text not null,
  scanned_at   timestamptz not null
);
