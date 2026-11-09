create table rsi_feedback (
  signal_id text primary key,
  user_id text not null references app_user(user_id) on delete cascade,
  event_id text not null unique references wiki_event(event_id) on delete cascade,
  eligible boolean not null,
  reviewed_fingerprint text,
  processed_at timestamptz
);
create index rsi_feedback_pending on rsi_feedback(user_id) where eligible and processed_at is null;
