alter table forge_candidate add column origin text not null default 'round';
create index forge_candidate_origin on forge_candidate (user_id, origin);

-- Voyager-style description vectors; tagged like every vector (decision 11): never compare tags.
create table forge_tool_vec (
  candidate_id  text not null references forge_candidate (id),
  embedder      text not null,
  model_version text not null,
  dim           integer not null,
  vec           real[] not null,
  primary key (candidate_id, model_version)
);

-- 'global' is the forge's shared scope name; no user may have it (spec §10).
alter table app_user add constraint app_user_not_global check (user_id <> 'global');
