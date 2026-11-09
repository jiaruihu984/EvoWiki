create table assistant_turn_stop (
  session_id   text not null references assistant_session (session_id),
  message_id   text not null,
  requested_at timestamptz not null,
  primary key (session_id, message_id)
);
