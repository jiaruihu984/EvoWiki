create table artifact (
  artifact_id     text primary key,
  user_id         text not null references app_user (user_id),
  kind            text not null,
  ref             text not null,               -- what it is a version of (page/section, node id …)
  version_key     text not null,               -- which version (a content hash, a rule rev …)
  produced_by_run text,
  created_at      timestamptz not null,
  unique (user_id, kind, ref, version_key)
);
create index artifact_user_ref on artifact (user_id, kind, ref);
create index artifact_run on artifact (produced_by_run) where produced_by_run is not null;

-- The DAG: a child version was produced from these parent versions (what its producer knew when it
-- saved it: the outline, nodes, rule revs, K cells, materials, a split's source node …). Never
-- guessed from time. `role` names the relation (outline / node / rule / k_cell / source /
-- split_from / merged_from / section / check / previous …).
create table artifact_dependency (
  child_id  text not null references artifact (artifact_id) on delete cascade,
  parent_id text not null references artifact (artifact_id) on delete cascade,
  role      text not null,
  primary key (child_id, parent_id, role),
  check (child_id <> parent_id)
);
create index artifact_dependency_parent on artifact_dependency (parent_id);
