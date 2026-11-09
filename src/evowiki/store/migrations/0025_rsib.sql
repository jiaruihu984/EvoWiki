create table rsi_b_detail (
  patch_id   text primary key references rsi_patch (patch_id),
  user_id    text not null check (user_id <> ''),
  detail     jsonb not null,
  updated_at timestamptz not null
);
create index rsi_b_detail_user on rsi_b_detail (user_id);

-- Signals a round used without leaving a patch (no acceptable proposal): not localized again.
create table rsi_b_consumed (
  user_id   text not null check (user_id <> ''),
  signal_id text not null,
  at        timestamptz not null,
  primary key (user_id, signal_id)
);
