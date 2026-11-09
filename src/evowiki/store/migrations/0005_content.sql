create table content_snapshot (
  id          text primary key,
  created_at  timestamptz not null,
  document    jsonb not null,
  imported_at timestamptz not null
);
create trigger content_snapshot_append_only before update or delete on content_snapshot
  for each row execute function evowiki_forbid_mutation();

create table content_active (
  singleton       boolean primary key default true check (singleton),
  content_version text not null references content_snapshot (id),
  activated_at    timestamptz not null
);

-- Dimension-agnostic (decision 11): each row names its model and dimension; searches compare
-- only rows of one (embedder_id, model_version). A new model = a full re-embed under its id.
create table segment_embedding (
  content_version text not null references content_snapshot (id),
  embedder_id     text not null,
  model_version   text not null,
  dim             int not null check (dim > 0),
  segment_id      text not null,
  concept_id      text not null,
  kind            text not null,
  lang            text not null,
  embedding       vector not null,
  embedded_at     timestamptz not null default clock_timestamp(),
  check (vector_dims(embedding) = dim),
  primary key (content_version, embedder_id, model_version, segment_id)
);
-- v1 scale (~3k segments) uses an exact cosine scan. If that grows, add one partial HNSW
-- index per model: create index ... using hnsw ((embedding::vector(<dim>)) vector_cosine_ops)
-- where embedder_id = '<id>' and model_version = '<version>'.
