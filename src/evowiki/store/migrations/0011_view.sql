create table view_entry (
  user_id    text not null references app_user (user_id),
  seq        int not null check (seq >= 0),
  entry      text not null check (entry in ('op', 'decision')),
  op_id      text not null,
  session_id text not null references user_session (session_id),
  ts         timestamptz not null,
  body       jsonb not null,
  primary key (user_id, seq)
);
create unique index view_entry_one_op on view_entry (op_id) where entry = 'op';
create unique index view_entry_one_decision on view_entry (op_id) where entry = 'decision';
create trigger view_entry_append_only before update or delete on view_entry
  for each row execute function evowiki_forbid_mutation();
create trigger view_entry_no_truncate before truncate on view_entry
  for each statement execute function evowiki_forbid_mutation();

create table lease_record (
  user_id        text not null references app_user (user_id),
  n              int not null check (n >= 0),
  kind           text not null check (kind in ('bridge_exposure', 'refractory_exposure')),
  concept_id     text not null,
  plan_id        text references render_plan (id),
  ts             timestamptz not null,
  after_view_seq int not null check (after_view_seq >= 0),
  primary key (user_id, n)
);
create trigger lease_record_append_only before update or delete on lease_record
  for each row execute function evowiki_forbid_mutation();
create trigger lease_record_no_truncate before truncate on lease_record
  for each statement execute function evowiki_forbid_mutation();

create table user_policy (
  user_id           text not null references app_user (user_id),
  n                 int not null check (n >= 0),
  policy_version_id text not null references policy_version (id),
  semantics         text not null,
  from_view_seq     int not null check (from_view_seq >= 0),
  created_at        timestamptz not null,
  primary key (user_id, n)
);
create trigger user_policy_append_only before update or delete on user_policy
  for each row execute function evowiki_forbid_mutation();
create trigger user_policy_no_truncate before truncate on user_policy
  for each statement execute function evowiki_forbid_mutation();

create table engine_state (
  user_id         text not null,
  run_no          int not null check (run_no >= 0),
  trigger         text not null check (trigger in ('interactions', 'session_end')),
  session_id      text not null references user_session (session_id),
  event_seq       bigint not null check (event_seq >= 0),
  view_seq_before int not null check (view_seq_before >= 0),
  view_seq_after  int not null check (view_seq_after >= view_seq_before),
  user_policy_n   int not null,
  status          text not null check (status in ('ok', 'failed')),
  state           jsonb,
  error           text,
  trace           jsonb,
  ran_at          timestamptz not null,
  primary key (user_id, run_no),
  foreign key (user_id, user_policy_n) references user_policy (user_id, n),
  check ((status = 'ok') = (state is not null and error is null)),
  check (status = 'ok' or view_seq_after = view_seq_before)
);
create trigger engine_state_append_only before update or delete on engine_state
  for each row execute function evowiki_forbid_mutation();
create trigger engine_state_no_truncate before truncate on engine_state
  for each statement execute function evowiki_forbid_mutation();
