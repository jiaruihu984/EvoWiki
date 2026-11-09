create index assistant_session_user_recent
  on assistant_session (user_id, created_at desc, session_id desc);
