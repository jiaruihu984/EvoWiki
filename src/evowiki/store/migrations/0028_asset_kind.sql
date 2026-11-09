alter table wiki_asset add column kind text not null default 'html' check (kind in ('html', 'evoui'));
