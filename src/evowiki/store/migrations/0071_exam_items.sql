alter table source_item drop constraint if exists source_item_kind_check;
alter table source_item add constraint source_item_kind_check
  check (kind in ('single','multi','tf','open','group'));
alter table source_item
  add column ord int not null default 0,
  add column number text,
  add column parent_id text references source_item(item_id) on delete cascade,
  add column status text not null default 'ok' check (status in ('ok','incomplete','unreadable')),
  add column problem text,
  add column attach text not null default 'pending' check (attach in ('pending','attached','unattached')),
  add column attach_note text,
  add column gen int not null default 0,
  add column created_at timestamptz not null default now();
create index source_item_user_source on source_item (user_id, source_id, ord);
create index source_item_user_node on source_item (user_id, node_id) where node_id is not null;

-- source_item_mark: a learner's self-report on an exam item (「我答对了 / 没答对 / 做完了」), one row per
-- key (unique: a resend is a duplicate): `exam:<item_id>:<YYYY-MM-DD>` for right / wrong (one graded
-- report a day), `exam:<item_id>:<YYYY-MM-DD>:done` for done; the day is the user's local day. No foreign key to
-- source_item: a re-extraction replaces the rows, the day's marks stay (the K evidence stays as well).
create table source_item_mark (
  user_id  text not null references app_user (user_id),
  item_id  text not null,
  mark_key text not null,
  result   text not null check (result in ('right','wrong','done')),
  day      date not null,
  at       timestamptz not null,
  unique (user_id, mark_key)
);
create index source_item_mark_day on source_item_mark (user_id, day, item_id);
