create table block_progress (
  user_id    text not null references app_user (user_id),
  asset_id   text not null references wiki_asset (asset_id),
  revision   integer not null check (revision >= 0),
  data       jsonb not null,
  updated_at timestamptz not null,
  primary key (user_id, asset_id)
);

-- block_event: one row per result event already graded / recorded (key, wikiapp.block_results
-- `event_key`: item `<node>:<item as JSON>:<attempt>:<revision>`, summary / route / request
-- `<node>:<type>:<attempt or 0>:<revision>`, prefixed `<session>|` when the event carries the
-- render's random session id — attempt / revision restart in a render without restored progress),
-- written in the same transaction as the evidence and
-- the block_result signal, so a resent event is skipped and never counted twice.
create table block_event (
  user_id   text not null references app_user (user_id),
  asset_id  text not null references wiki_asset (asset_id),
  event_key text not null,
  at        timestamptz not null,
  unique (user_id, asset_id, event_key)
);
