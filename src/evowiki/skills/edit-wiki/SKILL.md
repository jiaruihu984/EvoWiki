---
key: edit-wiki
name: Wiki 编辑
when: 改页面、重讲某一节、拆页合并、换顺序、整理 Wiki、拆点、改图谱关系
order: 40
refs:
  pages: {when: 改某一页、重讲或换个讲法讲某一节、按资料改页面或给这一节配图时, tools: [propose_page_edit, revise_page, illustrate_section, add_source]}
  path: {when: 拆页、合并、写新页、速查、调顺序、往后放时, tools: [read_path, split_page, merge_pages, write_page, cheat_sheet, reorder_path, postpone]}
  graph: {when: 拆点、改关系、挪点时，他要合并两个点时也读, tools: [read_graph, split_node, edit_relation, move_node]}
---
- 先读要做的那一类改动的参考（read_ref），它的工具随参考一起来。
