create table bot_settings (
  user_id    text primary key references app_user (user_id),
  settings   jsonb not null,
  updated_at timestamptz not null
);

-- Event cursor: how far the user's wiki log and K log have been turned into intents.
create table trigger_cursor (
  user_id    text primary key references app_user (user_id),
  wiki_seq   bigint not null default 0 check (wiki_seq >= 0),
  aspect_seq bigint not null default 0 check (aspect_seq >= 0),
  updated_at timestamptz not null
);

-- One intent produced by a rule: the start of every "why" chain.
create table bot_trigger (
  trigger_id   text primary key,
  user_id      text not null references app_user (user_id),
  kind         text not null,
  rule_id      text not null,
  rule_version int  not null,
  source_key   text not null,               -- what makes this intent unique (event id / slot date)
  dedup_key    text not null,               -- cooldown + suppression key
  route        text not null check (route in ('direct', 'plan', 'rsi')),
  evidence     jsonb not null,              -- ["wev_…", "aev_…"] event ids it rests on
  payload      jsonb not null,
  priority     int  not null,
  reason       text not null,               -- deterministic plain-Chinese reason
  title        text not null default '',    -- what the intent is about, for the 最近 list
  placement    text not null default 'today' check (placement in ('today', 'page')),
  page_id      text,                        -- the page a 'page' placement belongs to
  status       text not null default 'new'
               check (status in ('new', 'gated', 'awaiting_plan', 'planned', 'handed_off', 'failed')),
  gate_outcome text check (gate_outcome in ('pass', 'defer', 'skip')),
  gate_reason  text check (gate_reason in ('paused', 'kind_off', 'cap_reached', 'cooldown',
                                           'suppressed', 'empty', 'busy')),
  gate_detail  text not null default '',    -- the gate's plain-Chinese explanation of the outcome
  run_id       text,
  created_at   timestamptz not null,
  gated_at     timestamptz,
  unique (user_id, source_key)
);
create index bot_trigger_user_created on bot_trigger (user_id, created_at desc);
create index bot_trigger_new on bot_trigger (status, created_at) where status = 'new';

-- Daily slots: one row per user and local date once run or skipped.
create table bot_slot (
  user_id    text not null references app_user (user_id),
  slot_date  date not null,
  status     text not null check (status in ('enqueued', 'missed')),
  reason     text not null default '',
  slot_at    timestamptz not null,
  at         timestamptz not null,
  primary key (user_id, slot_date)
);

-- Event-check wake-ups (anti-loop flood guard reads the last minute).
create table bot_check (
  id      bigint generated always as identity primary key,
  user_id text not null references app_user (user_id),
  at      timestamptz not null,
  dropped boolean not null default false
);
create index bot_check_user_at on bot_check (user_id, at desc);

create table bot_suggestion (
  suggestion_id text primary key,
  user_id       text not null references app_user (user_id),
  trigger_id    text references bot_trigger (trigger_id),
  run_id        text,
  kind          text not null,
  title         text not null,
  why           text not null,                -- rule reason (+ planner reason)
  action        jsonb not null,               -- {"type": "open_page"|"start_practice"|"send_to_assistant"|"view_pending"|"open_settings", ...}
  placement     text not null check (placement in ('today', 'page')),
  page_id       text,
  priority      int  not null,
  status        text not null default 'pending'
                check (status in ('pending', 'shown', 'accepted', 'not_interested', 'expired',
                                  'done')),
  dedup_key     text not null,
  expires_at    timestamptz not null,
  cost_usd      double precision not null default 0,
  created_at    timestamptz not null,
  reacted_at    timestamptz,
  check (placement = 'today' or page_id is not null)
);
create index bot_suggestion_user_created on bot_suggestion (user_id, created_at desc);
create index bot_suggestion_user_dedup on bot_suggestion (user_id, dedup_key, created_at desc);

-- Learned suppression: explicit reactions per dedup key.
create table bot_suppression (
  user_id                text not null references app_user (user_id),
  dedup_key              text not null,
  accepted               int not null default 0,
  not_interested         int not null default 0,
  later                  int not null default 0,
  last_not_interested_at timestamptz,
  primary key (user_id, dedup_key)
);

-- One Bot run (a DBOS workflow): dispatch / plan / daily / check.
create table bot_run (
  run_id      text primary key,               -- DBOS workflow id
  user_id     text not null references app_user (user_id),
  kind        text not null,                  -- 'plan', 'daily', 'check', 'dispatch'
  trigger_id  text references bot_trigger (trigger_id),
  status      text not null check (status in ('running', 'ok', 'no_action', 'failed')),
  calls       int not null default 0,
  cost_usd    double precision not null default 0,
  reads       jsonb not null default '[]'::jsonb,   -- what the planner read
  detail      text not null default '',
  started_at  timestamptz not null,
  finished_at timestamptz
);
create index bot_run_user_kind on bot_run (user_id, kind, started_at desc);

-- A run kind that failed `failure_streak` times in a row pauses until the user's next daily slot.
create table bot_kind_pause (
  user_id text not null references app_user (user_id),
  kind    text not null check (kind in ('plan', 'check', 'daily', 'dispatch')),
  until   timestamptz not null,
  primary key (user_id, kind)
);
