create table harness_gate (
  gate_id   text primary key,
  user_id   text not null references app_user (user_id),
  round_id  text not null references harness_round (round_id),
  subject   text not null check (subject = 'diff' or subject ~ '^(tool|patch):fc_[A-Za-z0-9]{1,40}$'),
  verdict   text not null check (verdict in ('pass', 'thin', 'fail')),
  reasons   jsonb not null,
  detail    jsonb not null,
  at        timestamptz not null
);
create index harness_gate_round on harness_gate (round_id);
create trigger harness_gate_append_only before update or delete on harness_gate
  for each row execute function evowiki_forbid_mutation();

-- harness_patch_observation: the observation points of a landed forge code patch that a harness
-- round evaluated (its forge_patch.gate_ref is a harness_gate row with subject 'patch:<cid>'),
-- as harness_observation is for harness_change (which this table cannot reference: a patch has no
-- harness_change row). candidate_id is not a foreign key: forge_candidate is created by 0067.
-- Append-only, same trigger.
create table harness_patch_observation (
  obs_id       text primary key,
  seq          bigserial not null,
  user_id      text not null references app_user (user_id),
  candidate_id text not null check (candidate_id ~ '^fc_[A-Za-z0-9]{1,40}$'),
  at           timestamptz not null,
  verdict      text not null check (verdict in ('pending', 'helpful', 'harmful', 'no_effect')),
  deltas       jsonb not null,
  note         text not null default '',
  reason       text,
  dropped      jsonb not null default '{}'::jsonb,
  lesson_id    text references harness_experience (exp_id)
);
create index harness_patch_observation_cand on harness_patch_observation (candidate_id, at, seq);
create trigger harness_patch_observation_append_only before update or delete
  on harness_patch_observation for each row execute function evowiki_forbid_mutation();
