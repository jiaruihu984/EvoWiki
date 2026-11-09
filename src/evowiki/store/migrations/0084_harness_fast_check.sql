create table harness_fast_check (
  check_id  text primary key,
  seq       bigserial not null,
  user_id   text not null references app_user (user_id),
  change_id text not null references harness_change (change_id),
  at        timestamptz not null,
  outcome   text not null check (outcome in ('rolled_back', 'refused')),
  reason    text,
  evidence  jsonb not null,
  lesson_id text references harness_experience (exp_id)
);
create index harness_fast_check_change on harness_fast_check (change_id, at, seq);
create trigger harness_fast_check_append_only before update or delete on harness_fast_check
  for each row execute function evowiki_forbid_mutation();
