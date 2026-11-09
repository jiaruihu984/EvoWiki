create extension if not exists vector;

create or replace function evowiki_forbid_mutation() returns trigger
language plpgsql as $$
begin
  raise exception 'append-only table %: % is forbidden', TG_TABLE_NAME, TG_OP;
end $$;
