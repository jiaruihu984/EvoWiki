create table prompt_override (
  user_id    text not null references app_user (user_id),
  step       text not null,
  text       text not null,
  updated_at timestamptz not null,
  primary key (user_id, step)
);
