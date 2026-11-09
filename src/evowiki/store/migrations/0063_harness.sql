create table harness_value (
  user_id    text not null references app_user (user_id),
  key        text not null,
  value      jsonb not null,
  source     text not null check (source in ('user', 'improver', 'rollback')),
  version    integer not null,
  updated_at timestamptz not null,
  primary key (user_id, key)
);

create table harness_version (
  user_id    text not null references app_user (user_id),
  version    integer not null,
  key        text not null,
  before     jsonb,
  after      jsonb,
  source     text not null check (source in ('user', 'improver', 'rollback')),
  why        text not null,
  created_at timestamptz not null,
  primary key (user_id, version)
);

create trigger harness_version_append_only before update or delete on harness_version
  for each row execute function evowiki_forbid_mutation();

insert into harness_version (user_id, version, key, before, after, source, why, created_at)
select user_id, row_number() over (partition by user_id order by updated_at, step),
       'H1.' || step, null, to_jsonb(text), 'user', '你在 Prompt 设置里改的', updated_at
  from prompt_override;
insert into harness_value (user_id, key, value, source, version, updated_at)
select o.user_id, 'H1.' || o.step, to_jsonb(o.text), 'user', v.version, o.updated_at
  from prompt_override o
  join harness_version v on v.user_id = o.user_id and v.key = 'H1.' || o.step;
drop table prompt_override;
