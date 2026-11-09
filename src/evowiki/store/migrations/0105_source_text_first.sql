alter table source
  add column text_first boolean not null default false,
  add column imprecise_pages integer[] not null default '{}',
  add column toc_edited boolean not null default false;
