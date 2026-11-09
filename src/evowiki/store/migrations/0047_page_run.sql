create table page_run (
    run_id        text primary key,
    user_id       text not null,
    page_id       text not null,
    n             integer not null,
    kind          text not null check (kind in ('organize','prefetch','feedback','refine','rsi_rewrite')),
    origin        text not null,
    job           jsonb not null,
    status        text not null default 'writing' check (status in ('writing','done','failed')),
    started_at    timestamptz not null,
    finished_at   timestamptz,
    consumed_at   timestamptz,
    path_id       text,
    path_index    integer,
    fingerprint   text,
    held          jsonb,
    claimed_at    timestamptz,
    discarded_at  timestamptz,
    unique (user_id, page_id, n)
);
create index page_run_page on page_run (user_id, page_id, n desc);
create unique index page_run_one_prefetch on page_run (user_id, path_id)
    where path_id is not null and claimed_at is null and discarded_at is null;
