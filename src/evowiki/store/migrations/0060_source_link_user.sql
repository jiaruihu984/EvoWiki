alter table source_link drop constraint source_link_pkey;
alter table source_link add primary key (user_id, node_id, section_id, role);
alter table source_link drop constraint source_link_role_check;
alter table source_link add constraint source_link_role_check
  check (role in ('teaches','exercises','reads'));
create index source_link_section_idx on source_link (section_id);
-- 资料第 3+4 期（Task 14）：印的页码偏移（PDF 第几页 − 印的页码；null = 没核对出来，显示 PDF 页）
-- 和导入时抽出的符号表 [{"term", "means", "page"}]。
alter table source add column page_offset int;
alter table source add column glossary jsonb not null default '[]';
