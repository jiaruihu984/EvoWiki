create table render_plan (
  id                  text primary key,
  user_id             text not null references app_user (user_id),
  session_id          text not null references user_session (session_id),
  concept_id          text not null,
  kind                text not null check (kind in ('page', 'answer')),
  condition           text not null,
  policy_version      text not null references policy_version (id),
  content_version     text not null references content_snapshot (id),
  view_version        text,
  ts                  timestamptz not null,
  lang                text not null,
  plan                jsonb not null,
  bridge_llm_call_ids text[] not null default '{}',
  outcome_labels      text[] not null default '{}',
  labeled_at          timestamptz,
  ask_event_id        text references event (event_id),
  check ((kind = 'answer') = (ask_event_id is not null))
);
create index render_plan_session on render_plan (session_id, ts, id);

create function render_plan_guard() returns trigger language plpgsql as $$
begin
  if TG_OP = 'DELETE' then
    raise exception 'append-only table render_plan: DELETE is forbidden';
  end if;
  if (NEW.id, NEW.user_id, NEW.session_id, NEW.concept_id, NEW.kind, NEW.condition,
      NEW.policy_version, NEW.content_version, NEW.view_version, NEW.ts, NEW.lang, NEW.plan,
      NEW.ask_event_id)
     is distinct from
     (OLD.id, OLD.user_id, OLD.session_id, OLD.concept_id, OLD.kind, OLD.condition,
      OLD.policy_version, OLD.content_version, OLD.view_version, OLD.ts, OLD.lang, OLD.plan,
      OLD.ask_event_id) then
    raise exception 'render_plan decision fields are immutable';
  end if;
  if NEW.bridge_llm_call_ids[1:cardinality(OLD.bridge_llm_call_ids)]
     is distinct from OLD.bridge_llm_call_ids then
    raise exception 'render_plan.bridge_llm_call_ids is append-only';
  end if;
  return NEW;
end $$;
create trigger render_plan_guard before update or delete on render_plan
  for each row execute function render_plan_guard();

create table bridge_check (
  id               bigserial primary key,
  render_plan_id   text not null references render_plan (id),
  llm_call_id      text references llm_call (id),
  after_segment_id text,
  sentence         text not null,
  cites            text[] not null,
  passed           boolean not null,
  reason           text,
  ts               timestamptz not null
);
create trigger bridge_check_append_only before update or delete on bridge_check
  for each row execute function evowiki_forbid_mutation();

alter table event add constraint event_render_plan_fk
  foreign key (render_plan_id) references render_plan (id);
