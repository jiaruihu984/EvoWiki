create table refine_job (
  id       bigint generated always as identity primary key,
  job_id   text not null,
  user_id  text not null references app_user (user_id),
  page_id  text not null,
  job      jsonb not null,
  ts       timestamptz not null default clock_timestamp()
);
create index refine_job_page on refine_job (user_id, page_id, id desc);
create index refine_job_id on refine_job (job_id, id desc);
create trigger refine_job_append_only before update or delete on refine_job
  for each row execute function evowiki_forbid_mutation();
create trigger refine_job_no_truncate before truncate on refine_job
  for each statement execute function evowiki_forbid_mutation();

-- wiki_asset: an interactive committed by a refine (standalone HTML, its render check and the
-- section it belongs to). Written in the same transaction as the page it is placed on.
create table wiki_asset (
  asset_id       text primary key,
  user_id        text not null references app_user (user_id),
  page_id        text not null,
  section_id     text not null,
  interactive_id text not null,
  title          text not null,
  height         integer not null check (height between 400 and 1600),
  sha256         text not null check (sha256 ~ '^[0-9a-f]{64}$'),
  html           text not null,
  render_check   jsonb not null,
  job_id         text not null,
  created_at     timestamptz not null
);
create index wiki_asset_page on wiki_asset (user_id, page_id);
create trigger wiki_asset_append_only before update or delete on wiki_asset
  for each row execute function evowiki_forbid_mutation();
create trigger wiki_asset_no_truncate before truncate on wiki_asset
  for each statement execute function evowiki_forbid_mutation();
