create table bot_goal_review (
  user_id    text not null references app_user (user_id),
  week_start date not null,
  path_id    text not null,
  situation  text not null,
  facts      jsonb not null,
  trigger_id text references bot_trigger (trigger_id),
  at         timestamptz not null,
  primary key (user_id, week_start, path_id)
);

-- A parked goal: no goal_idle card and no weekly review until resumed. The goal and its pages are
-- untouched.
create table bot_goal_state (
  user_id    text not null references app_user (user_id),
  path_id    text not null,
  parked_at  timestamptz not null,
  resumed_at timestamptz,
  primary key (user_id, path_id)
);

-- Active reading time the plugin reports (visible + focused + recently used), summed per user,
-- page and local day (the Bot settings' time zone).
create table reading_time (
  user_id    text not null references app_user (user_id),
  page_id    text not null,
  day        date not null,
  seconds    int not null check (seconds >= 0),
  updated_at timestamptz not null,
  primary key (user_id, page_id, day)
);

-- Batches already counted: a resent batch is not counted twice.
create table reading_batch (
  user_id  text not null references app_user (user_id),
  batch_id text not null,
  at       timestamptz not null,
  primary key (user_id, batch_id)
);
