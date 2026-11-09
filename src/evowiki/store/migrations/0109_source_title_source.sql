alter table source
  add column title_source text not null default 'given'
    check (title_source in ('given', 'document', 'fallback')),
  add column earlier_titles text[] not null default '{}';
update source set title_source = 'fallback' where title = origin;
