create trigger evidence_no_truncate before truncate on evidence
  for each statement execute function evowiki_forbid_mutation();
create trigger extraction_output_no_truncate before truncate on extraction_output
  for each statement execute function evowiki_forbid_mutation();
create trigger extractor_version_no_truncate before truncate on extractor_version
  for each statement execute function evowiki_forbid_mutation();
create trigger extraction_job_no_truncate before truncate on extraction_job
  for each statement execute function evowiki_forbid_mutation();
create trigger llm_call_no_truncate before truncate on llm_call
  for each statement execute function evowiki_forbid_mutation();
create trigger llm_attempt_no_truncate before truncate on llm_attempt
  for each statement execute function evowiki_forbid_mutation();
create trigger content_snapshot_no_truncate before truncate on content_snapshot
  for each statement execute function evowiki_forbid_mutation();
create trigger policy_version_no_truncate before truncate on policy_version
  for each statement execute function evowiki_forbid_mutation();
create trigger bridge_check_no_truncate before truncate on bridge_check
  for each statement execute function evowiki_forbid_mutation();
create trigger reconcile_report_no_truncate before truncate on reconcile_report
  for each statement execute function evowiki_forbid_mutation();
