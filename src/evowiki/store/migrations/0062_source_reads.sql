alter table source add column run_gen int not null default 0;   -- 导入跑了第几代（续跑、看图补读各 +1）
alter table source add column vision_quote jsonb;                -- {pages:[..], usd, model}

create table source_page_read (
  source_id  text not null references source (source_id) on delete cascade,
  user_id    text not null references app_user (user_id),
  page       int  not null,
  text       text,                                  -- null：读过但没读出来
  by         text not null check (by in ('local', 'claude')),
  figure     boolean not null default false,        -- 图表 / 几何图：交给会看图的模型
  created_at timestamptz not null,
  primary key (source_id, page, by)
);
create index source_page_read_user on source_page_read (user_id, source_id);

-- 资料先能用、再算向量：块先存文字（BM25 就能搜到），向量在后台补上。
alter table source_chunk alter column embedder_id drop not null;
alter table source_chunk alter column model_version drop not null;
alter table source_chunk alter column dim drop not null;
alter table source_chunk alter column embedding drop not null;
alter table source_chunk add constraint source_chunk_vector_whole check (
  (embedding is null and embedder_id is null and model_version is null and dim is null)
  or (embedding is not null and embedder_id is not null and model_version is not null
      and dim is not null));
