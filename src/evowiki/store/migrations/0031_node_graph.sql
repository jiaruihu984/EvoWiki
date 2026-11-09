create table node_graph_backup (
  id       bigserial primary key,
  user_id  text not null references app_user (user_id),
  taken_at timestamptz not null default clock_timestamp(),
  legacy   jsonb not null
);
create index node_graph_backup_user on node_graph_backup (user_id, id desc);
create trigger node_graph_backup_append_only before update or delete on node_graph_backup
  for each row execute function evowiki_forbid_mutation();
create trigger node_graph_backup_no_truncate before truncate on node_graph_backup
  for each statement execute function evowiki_forbid_mutation();
