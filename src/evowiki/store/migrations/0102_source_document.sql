create table source_document (
  source_id  text primary key references source (source_id) on delete cascade,
  user_id    text not null references app_user (user_id),
  backend    text not null,
  version    text not null,
  doc        jsonb not null,
  created_at timestamptz not null
);
