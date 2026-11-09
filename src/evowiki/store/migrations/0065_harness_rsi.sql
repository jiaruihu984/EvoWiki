create table harness_round (
  round_id        text primary key,
  user_id         text not null references app_user (user_id),
  started_at      timestamptz not null,
  finished_at     timestamptz,
  status          text not null check (status in ('running', 'applied', 'asked', 'thin', 'failed', 'no_change',
                                             'skipped', 'error')),
  reason          text,
  finding         text,
  signal_ids      jsonb,
  cases           jsonb,
  candidates      jsonb,
  comparisons     jsonb,
  decision        jsonb,
  diff            jsonb,
  cost            double precision,
  improver_run_id text
);
create index harness_round_user on harness_round (user_id, started_at desc);

create table harness_change (
  change_id   text primary key,
  user_id     text not null references app_user (user_id),
  round_id    text not null references harness_round (round_id),
  version     integer,
  diff        jsonb not null,
  status      text not null check (status in ('applied', 'asked', 'rejected', 'undone', 'rolled_back')),
  cost_before double precision,
  cost_after  double precision,
  reason      text,
  created_at  timestamptz not null,
  settled_at  timestamptz
);
create index harness_change_user on harness_change (user_id, created_at desc);

create table harness_experience (
  exp_id     text primary key,
  user_id    text not null references app_user (user_id),
  situation  jsonb not null,
  change     jsonb not null,
  evidence   jsonb not null,
  verdict    text not null check (verdict in ('effective', 'ineffective', 'harmful')),
  scope      text not null check (scope in ('user', 'general')),
  created_at timestamptz not null,
  weight     double precision not null
);
create index harness_experience_user on harness_experience (user_id, created_at desc);

create or replace function harness_round_guard() returns trigger
language plpgsql as $$
begin
  if old.status <> 'running' or new.status not in ('applied', 'asked', 'thin', 'failed', 'no_change',
                                                 'skipped', 'error') then
    raise exception 'harness_round %: status % -> % is forbidden', old.round_id, old.status, new.status;
  end if;
  if new.finished_at is null
     or (new.round_id, new.user_id, new.started_at, new.finding)
        is distinct from (old.round_id, old.user_id, old.started_at, old.finding) then
    raise exception 'harness_round %: only the result columns may be set when it finishes', old.round_id;
  end if;
  return new;
end $$;

create or replace function harness_change_guard() returns trigger
language plpgsql as $$
begin
  if not ((old.status = 'asked' and new.status in ('applied', 'rejected'))
          or (old.status = 'applied' and new.status in ('undone', 'rolled_back'))) then
    raise exception 'harness_change %: status % -> % is forbidden', old.change_id, old.status, new.status;
  end if;
  if new.settled_at is null
     or (new.change_id, new.user_id, new.round_id, new.diff, new.cost_before, new.cost_after,
         new.reason, new.created_at)
        is distinct from (old.change_id, old.user_id, old.round_id, old.diff, old.cost_before,
                          old.cost_after, old.reason, old.created_at)
     or (new.version is distinct from old.version
         and not (old.status = 'asked' and new.status = 'applied')) then
    raise exception 'harness_change %: only status, settled_at and (on apply) version may change',
      old.change_id;
  end if;
  return new;
end $$;

create trigger harness_round_settle before update on harness_round
  for each row execute function harness_round_guard();
create trigger harness_round_no_delete before delete on harness_round
  for each row execute function evowiki_forbid_mutation();
create trigger harness_change_settle before update on harness_change
  for each row execute function harness_change_guard();
create trigger harness_change_no_delete before delete on harness_change
  for each row execute function evowiki_forbid_mutation();
create trigger harness_experience_append_only before update or delete on harness_experience
  for each row execute function evowiki_forbid_mutation();

-- the harness round's own calls (the improver, the judge) are metered in their own usage group;
-- its replays stay in 'page' (the writer's real cost)
alter table usage_record drop constraint usage_record_grp_check;
alter table usage_record add constraint usage_record_grp_check
  check (grp in ('page', 'assistant', 'curator', 'diagnostic', 'review', 'other', 'proactive',
                 'rsi'));

-- the new default RSI cycle trigger (RsiSettings.cycle = every_10_feedback, design §5.1)
alter table rsi_cycle drop constraint rsi_cycle_trigger_check;
alter table rsi_cycle add constraint rsi_cycle_trigger_check
  check (trigger in ('manual', 'every_5_feedback', 'every_10_feedback', 'weekly', 'bot'));

-- the lazy rewrite of a page a harness change marked has its own run kind
-- (keeps materials 0061's kind source_revise, claude/gifted-euclid-9c22fd)
alter table page_run drop constraint page_run_kind_check;
alter table page_run add constraint page_run_kind_check
  check (kind in ('organize','prefetch','feedback','refine','rsi_rewrite','reorder_rewrite',
                  'harness_rewrite','source_revise'));

-- the observation of an applied harness change (§5.7): one point each time its after-side evidence
-- grew by MIN_EVENTS (the verdict on the anchor metrics then, `harness.observe`); two harmful points
-- in a row with growing evidence roll the change back. `lesson_id`: the lesson this point wrote.
-- `note`: '', 'auto_rollback' (the observation rolled the change back) or 'rollback_refused'
-- (it should have, the undo was refused: `reason` says why). `dropped`: what the cost / latency
-- metrics left out, counted per side (page jobs with an unpriced call or an unfinished round).
create table harness_observation (
  obs_id    text primary key,
  seq       bigserial not null,
  user_id   text not null references app_user (user_id),
  change_id text not null references harness_change (change_id),
  at        timestamptz not null,
  verdict   text not null check (verdict in ('pending', 'helpful', 'harmful', 'no_effect')),
  deltas    jsonb not null,
  note      text not null default '',
  reason    text,
  dropped   jsonb not null default '{}'::jsonb,
  lesson_id text references harness_experience (exp_id)
);
create index harness_observation_change on harness_observation (change_id, at, seq);
create trigger harness_observation_append_only before update or delete on harness_observation
  for each row execute function evowiki_forbid_mutation();
