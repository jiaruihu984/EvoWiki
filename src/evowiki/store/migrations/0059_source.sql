create table source (
  source_id   text primary key,
  user_id     text not null references app_user (user_id),
  kind        text not null check (kind in ('pdf','pptx','docx','xlsx','image','md','text','url')),
  title       text not null,
  sha256      text not null,
  bytes       bigint not null check (bytes >= 0),
  pages       int,
  status      text not null check (status in ('reading','ready','partial','failed')),
  error       text,
  unreadable_pages int[] not null default '{}',
  toc_mode    text check (toc_mode in ('outline','headings','llm','pages')),
  progress    jsonb not null default '{}',
  origin      text,
  created_at  timestamptz not null,
  updated_at  timestamptz not null,
  deleted_at  timestamptz
);
create index source_user_idx on source (user_id, created_at desc);
create index source_sha_idx on source (user_id, sha256) where deleted_at is null;

create table source_file (
  source_id text primary key references source (source_id) on delete cascade,
  mime      text not null,
  data      bytea not null
);

create table source_section (
  section_id  text primary key,
  source_id   text not null references source (source_id) on delete cascade,
  parent_id   text references source_section (section_id) on delete cascade,
  ord         int not null,
  depth       int not null default 0,
  title       text not null,
  page_from   int, page_to int,
  text        text not null,
  tokens      int not null,
  summary     text,
  summary_hash text,
  read_by     text not null check (read_by in ('text','vision','none')),
  embedder_id text, model_version text, dim int,
  embedding   vector,
  check (embedding is null or vector_dims(embedding) = dim)
);
create index source_section_src_idx on source_section (source_id, ord);

create table source_chunk (
  chunk_id    text primary key,
  section_id  text not null references source_section (section_id) on delete cascade,
  source_id   text not null references source (source_id) on delete cascade,
  ord         int not null,
  page        int,
  text        text not null,
  embedder_id text not null, model_version text not null, dim int not null check (dim > 0),
  embedding   vector not null,
  check (vector_dims(embedding) = dim)
);
create index source_chunk_src_idx on source_chunk (source_id);

create table source_use (
  use_id     text primary key,
  source_id  text not null references source (source_id) on delete cascade,
  user_id    text not null references app_user (user_id),
  use        text not null check (use in ('learn','understand','ask','supplement','exam')),
  goal_id    text, page_id text, session_id text,
  created_at timestamptz not null
);
create index source_use_src_idx on source_use (source_id);

create table source_link (
  user_id    text not null references app_user (user_id),
  node_id    text not null,
  section_id text not null references source_section (section_id) on delete cascade,
  role       text not null check (role in ('teaches','exercises')),
  by         text not null check (by in ('intake','curator','user')),
  created_at timestamptz not null,
  primary key (node_id, section_id, role)
);

create table source_item (
  item_id     text primary key,
  user_id     text not null references app_user (user_id),
  source_id   text not null references source (source_id) on delete cascade,
  section_id  text references source_section (section_id) on delete set null,
  page        int,
  kind        text not null check (kind in ('single','multi','tf','open')),
  stem        text not null,
  options     jsonb not null default '[]',
  answer      text,
  explanation text,
  node_id     text,
  cell        text,
  has_key     boolean not null
);
