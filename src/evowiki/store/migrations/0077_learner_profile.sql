create table learner_profile (
  user_id    text primary key references app_user (user_id),
  body       jsonb not null,
  updated_at timestamptz not null
);
