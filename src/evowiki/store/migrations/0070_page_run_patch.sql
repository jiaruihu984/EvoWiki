alter table page_run drop constraint page_run_kind_check;
alter table page_run add constraint page_run_kind_check
  check (kind in ('organize','prefetch','feedback','refine','rsi_rewrite','reorder_rewrite',
                  'harness_rewrite','source_revise','patch_rewrite'));
