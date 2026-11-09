alter table source_link drop constraint source_link_by_check;
alter table source_link add constraint source_link_by_check
  check (by in ('intake','curator','user','blocks','enter'));
