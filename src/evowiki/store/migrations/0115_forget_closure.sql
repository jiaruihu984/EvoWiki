create table memory_dependency (
  user_id text not null,
  source text not null,
  target text not null,
  owner text not null,
  primary key (user_id, source, target, owner)
);
create index memory_dependency_owner on memory_dependency(user_id, owner);
create index memory_dependency_target on memory_dependency(user_id, target);
create table forget_frontier (
  request_id text not null references forget_request(request_id),
  node text not null,
  cause text not null,
  expanded boolean not null default false,
  primary key(request_id, node)
);
create index forget_frontier_pending on forget_frontier(request_id, node) where not expanded;
create index forget_frontier_source on forget_frontier(node, request_id);

create function evowiki_index_memory_entry(sid text, eid text, n bigint, typ text, p jsonb)
returns void language plpgsql as $$
declare uid text; mid text; dst text; ref text; x jsonb;
begin
  select user_id into uid from assistant_session where session_id=sid;
  if uid is null then return; end if;
  delete from memory_dependency where user_id=uid and owner='entry:' || eid;
  if typ='user' then mid:=p->>'message_id';
  elsif typ='assistant' then
    select payload->>'message_id' into mid from assistant_entry
      where session_id=sid and seq<n and type='user' order by seq desc limit 1;
  else mid:=p->>'message_id'; end if;
  if mid is null then return; end if;
  dst:='turn:' || sid || '/' || mid;
  if typ in ('user','assistant') then
    insert into memory_dependency values(uid,'message:' || sid || '/' || (p->>'message_id'),dst,'entry:' || eid)
      on conflict do nothing;
  end if;
  insert into memory_dependency values(uid,'session:' || sid,dst,'entry:' || eid)
    on conflict do nothing;
  insert into memory_dependency values(uid,dst,'ep:' || sid || '/' || mid,'entry:' || eid)
    on conflict do nothing;
  if typ in ('user','assistant') then
    insert into memory_dependency values(uid,dst,'message:' || sid || '/' || (p->>'message_id'),'entry:' || eid)
      on conflict do nothing;
  end if;
  if typ='assistant' then
    for x in select value from jsonb_array_elements(coalesce(p->'recalled','[]')) loop
      if x->>'deleted'='true' then continue; end if;
      if x->>'id' is not null then
        insert into memory_dependency values(uid,x->>'id',dst,'entry:' || eid) on conflict do nothing;
      end if;
      for ref in select jsonb_array_elements_text(coalesce(x->'source_nodes','[]')) loop
        insert into memory_dependency values(uid,ref,dst,'entry:' || eid) on conflict do nothing;
      end loop;
    end loop;
  elsif typ='tool' and p->>'name' in ('search_memory','read_past_turn') then
    for ref in select jsonb_array_elements_text(coalesce(p->'memory_ids','[]')) loop
      insert into memory_dependency values(uid,ref,dst,'entry:' || eid) on conflict do nothing;
    end loop;
    ref:=p->'args'->>'memory_id';
    if ref is not null then
      insert into memory_dependency values(uid,ref,dst,'entry:' || eid) on conflict do nothing;
    end if;
    if p->'args'->>'session_id' is not null and p->'args'->>'message_id' is not null then
      ref:='ep:' || (p->'args'->>'session_id') || '/' || (p->'args'->>'message_id');
      insert into memory_dependency values(uid,ref,dst,'entry:' || eid) on conflict do nothing;
    end if;
    for ref in select jsonb_array_elements_text(coalesce(p->'sources_sessions','[]')) loop
      insert into memory_dependency values(uid,'session:' || ref,dst,'entry:' || eid) on conflict do nothing;
    end loop;
  end if;
  if typ='tool' then
    for ref in select jsonb_array_elements_text(coalesce(p->'source_nodes','[]')) loop
      insert into memory_dependency values(uid,ref,dst,'entry:' || eid) on conflict do nothing;
    end loop;
  end if;
end $$;

create function evowiki_memory_entry_index() returns trigger language plpgsql as $$
begin
  if tg_op='DELETE' then
    delete from memory_dependency where owner='entry:' || old.entry_id;
    return old;
  end if;
  perform evowiki_index_memory_entry(new.session_id,new.entry_id,new.seq,new.type,new.payload);
  return new;
end $$;
create trigger memory_entry_index after insert or update or delete on assistant_entry
  for each row execute function evowiki_memory_entry_index();

create function evowiki_index_memory_item(uid text, iid text, sid text, evidence jsonb)
returns void language plpgsql as $$
declare x jsonb; src text; mid text;
begin
  delete from memory_dependency where user_id=uid and owner='item:' || iid;
  if sid is not null then
    insert into memory_dependency values(uid,'session:' || sid,'item:' || iid,'item:' || iid)
      on conflict do nothing;
  end if;
  for x in select value from jsonb_array_elements(coalesce(evidence,'[]')) loop
    if x->>'session_id' is null or x->>'message_id' is null then continue; end if;
    select u.payload->>'message_id' into mid from assistant_entry e
      join assistant_entry u on u.session_id=e.session_id and u.seq<=e.seq and u.type='user'
      where e.session_id=x->>'session_id' and e.payload->>'message_id'=x->>'message_id'
      and e.type in ('user','assistant') order by u.seq desc limit 1;
    src:=case when mid is not null then 'turn:' || (x->>'session_id') || '/' || mid
      else 'message:' || (x->>'session_id') || '/' || (x->>'message_id') end;
    insert into memory_dependency values(uid,src,'item:' || iid,'item:' || iid) on conflict do nothing;
    insert into memory_dependency values(uid,'session:' || (x->>'session_id'),'item:' || iid,'item:' || iid)
      on conflict do nothing;
  end loop;
end $$;
create function evowiki_memory_item_index() returns trigger language plpgsql as $$
begin
  if tg_op='DELETE' then
    delete from memory_dependency where user_id=old.user_id and owner='item:' || old.item_id;
    return old;
  end if;
  perform evowiki_index_memory_item(new.user_id,new.item_id,new.session_id,new.evidence);
  return new;
end $$;
create trigger memory_item_index after insert or update or delete on memory_item
  for each row execute function evowiki_memory_item_index();

-- One-time indexing of existing product records, without regenerating their content.
select evowiki_index_memory_entry(session_id,entry_id,seq,type,payload)
  from assistant_entry order by session_id,seq;
select evowiki_index_memory_item(user_id,item_id,session_id,evidence) from memory_item;

-- Publication and a forget request serialize on this same per-user lock. An in-flight
-- model response cannot commit derived content after the request has become visible.
create function evowiki_memory_write_fence() returns trigger language plpgsql as $$
declare uid text;
begin
  if public.evowiki_forgetting() then return new; end if;
  if tg_table_name='assistant_entry' then
    if new.type not in ('assistant','tool','compaction','consolidation') then return new; end if;
    select user_id into uid from assistant_session where session_id=new.session_id;
  else uid:=new.user_id; end if;
  perform pg_advisory_xact_lock(hashtext('memory:user:' || uid));
  if exists(select from forget_request where user_id=uid and state<>'committed') then
    raise exception 'memory writes paused while forgetting for user %',uid;
  end if;
  return new;
end $$;
create trigger memory_entry_write_fence before insert or update on assistant_entry
  for each row execute function evowiki_memory_write_fence();
create trigger memory_item_write_fence before insert or update on memory_item
  for each row execute function evowiki_memory_write_fence();
create trigger memory_episode_write_fence before insert or update on memory_episode
  for each row execute function evowiki_memory_write_fence();

-- Request creation fences all publishers, including callers outside ForgetService.
create function evowiki_memory_request_fence() returns trigger language plpgsql as $$
begin
  perform pg_advisory_xact_lock(hashtext('memory:user:' || new.user_id));
  return new;
end $$;
create trigger memory_request_fence before insert on forget_request
  for each row execute function evowiki_memory_request_fence();

-- Reversible suppression hides derived answers without deleting original conversation text.
create table memory_suppression_closure (
  user_id text not null, owner text not null, node text not null,
  primary key(user_id,owner,node)
);
create index memory_suppression_closure_node on memory_suppression_closure(user_id,node);

-- Provenance is metadata: an in-flight call must still register after a request arrives.
-- The same user lock prevents graph publication from racing the final deleting transaction.
create function evowiki_memory_dependency_fence() returns trigger language plpgsql as $$
begin
  perform pg_advisory_xact_lock(hashtext('memory:user:' || new.user_id));
  return new;
end $$;
create trigger memory_dependency_write_fence before insert or update on memory_dependency
  for each row execute function evowiki_memory_dependency_fence();
create function evowiki_memory_dependency_frontier() returns trigger language plpgsql as $$
begin
  insert into forget_frontier(request_id,node,cause)
    select f.request_id,new.target,new.source from forget_frontier f
    join forget_request r on r.request_id=f.request_id
    where r.user_id=new.user_id and r.state<>'committed' and f.node=new.source
    on conflict do nothing;
  with recursive hidden(owner,node) as (
    select owner,new.target from memory_suppression_closure
      where user_id=new.user_id and node=new.source
    union
    select h.owner,d.target from hidden h join memory_dependency d on d.source=h.node
      where d.user_id=new.user_id
  ) insert into memory_suppression_closure(user_id,owner,node)
    select new.user_id,owner,node from hidden on conflict do nothing;
  return new;
end $$;
create trigger memory_dependency_frontier after insert on memory_dependency
  for each row execute function evowiki_memory_dependency_frontier();
-- Existing suppressions obey the same provenance policy after upgrade. Preserve an active
-- fact action's owner so its original undo can remove all descendant masks as well.
with recursive roots(user_id,owner,node) as (
  select s.user_id,coalesce(a.action_id,'suppression:' || s.item_id),
         case when s.kind='episode' then s.item_id else 'item:' || s.item_id end
  from memory_suppression s left join agent_action a on a.user_id=s.user_id
    and a.kind='memory_forget' and a.status='executed' and (a.result->'keys') ? s.item_id
  union
  select s.user_id,coalesce(a.action_id,'suppression:' || s.item_id),
         'message:' || e.session_id || '/' || (e.payload->>'message_id')
  from memory_suppression s join assistant_session ss on ss.user_id=s.user_id
    join assistant_entry e on e.session_id=ss.session_id
      and e.type in ('user','assistant')
      and (e.payload->>'message_id')=any(s.evidence_message_ids)
    left join agent_action a on a.user_id=s.user_id and a.kind='memory_forget'
      and a.status='executed' and (a.result->'keys') ? s.item_id
), reached(user_id,owner,node) as (
  select user_id,owner,node from roots
  union
  select r.user_id,r.owner,d.target from reached r
    join memory_dependency d on d.user_id=r.user_id and d.source=r.node
)
insert into memory_suppression_closure(user_id,owner,node)
  select user_id,owner,node from reached on conflict do nothing;
-- Round gate explanations are generated material too; retain verdict and identity for audit.
drop trigger harness_gate_append_only on harness_gate;
create trigger harness_gate_append_only before update or delete on harness_gate
  for each row execute function evowiki_forget_guard('reasons','detail');
-- Failed provider attempts and failed traced calls carry complete program-recorded inputs.
create function evowiki_memory_source_user(p jsonb) returns text language plpgsql as $$
declare uid text; owners text[]; nodes text[];
begin
 nodes:=array(select jsonb_array_elements_text(coalesce(p->'source_nodes','[]')));
 if cardinality(nodes)=0 then return null; end if;
 uid:=p->>'source_user_id';
 if uid is not null then
  if not exists(select 1 from app_user where user_id=uid) then raise exception 'unknown provenance user %',uid; end if;
  return uid;
 end if;
 select array_agg(distinct u.user_id) into owners from (
  select user_id from memory_dependency where source=any(nodes) or target=any(nodes)
  union select user_id from dream_source where source_id=any(nodes)
  union select r.user_id from forget_frontier f join forget_request r using(request_id) where f.node=any(nodes)
  union select user_id from assistant_session where session_id in (
   select case when starts_with(n,'session:') then substring(n from 9)
    else split_part(substring(n from position(':' in n)+1),'/',1) end
   from unnest(nodes) n where n like 'session:%' or n like 'turn:%' or n like 'ep:%' or n like 'message:%')
  union select user_id from memory_item where 'item:'||item_id=any(nodes)
  union select user_id from harness_round where 'round:'||round_id=any(nodes)
  union select user_id from harness_change where 'change:'||change_id=any(nodes)
  union select user_id from harness_experience where 'experience:'||exp_id=any(nodes)
  union select user_id from harness_observation where 'observation:'||obs_id=any(nodes)
 ) u;
 if cardinality(owners)=1 then return owners[1]; end if;
 raise exception 'provenance inputs must identify exactly one user';
end $$;
create function evowiki_index_memory_attempt(aid text,p jsonb) returns void language plpgsql as $$
declare uid text; src text;
begin
 uid:=evowiki_memory_source_user(p);
 if uid is null then return; end if;
 for src in select jsonb_array_elements_text(p->'source_nodes') loop
  insert into memory_dependency values(uid,src,'attempt:'||aid,'attempt:'||aid) on conflict do nothing;
 end loop;
end $$;
create function evowiki_memory_attempt_index() returns trigger language plpgsql as $$
begin perform evowiki_index_memory_attempt(new.id,new.params); return new; end $$;
create trigger memory_attempt_index after insert on llm_attempt for each row execute function evowiki_memory_attempt_index();
create function evowiki_memory_call_index() returns trigger language plpgsql as $$
declare uid text; src text;
begin
 uid:=coalesce(new.user_id,evowiki_memory_source_user(new.params));
 if uid is null then return new; end if;
 for src in select jsonb_array_elements_text(coalesce(new.params->'source_nodes','[]')) loop
  insert into memory_dependency values(uid,src,'call:'||new.id,'call:'||new.id) on conflict do nothing;
 end loop;
 return new;
end $$;
create trigger memory_call_index after insert on llm_call for each row execute function evowiki_memory_call_index();
create function evowiki_index_memory_step(rid text,n bigint,p jsonb,cid text) returns void language plpgsql as $$
declare uid text; src text; nodes jsonb;
begin
 select user_id into uid from agent_run where run_id=rid;
 nodes:=coalesce(p->'source_nodes',p->'params'->'source_nodes','[]');
 if uid is null or jsonb_array_length(nodes)=0 then return; end if;
 for src in select jsonb_array_elements_text(nodes) loop
  insert into memory_dependency values(uid,src,'run:'||rid,'step:'||rid||':'||n) on conflict do nothing;
 end loop;
 if cid is not null then
  insert into memory_dependency values(uid,'run:'||rid,'call:'||cid,'step:'||rid||':'||n) on conflict do nothing;
 end if;
end $$;
create function evowiki_memory_step_index() returns trigger language plpgsql as $$
begin perform evowiki_index_memory_step(new.run_id,new.seq,new.payload,new.call_id); return new; end $$;
create trigger memory_step_index after insert on agent_step for each row execute function evowiki_memory_step_index();
select evowiki_index_memory_attempt(id,params) from llm_attempt where jsonb_array_length(coalesce(params->'source_nodes','[]'))>0;
select evowiki_index_memory_step(run_id,seq,payload,call_id) from agent_step
 where jsonb_array_length(coalesce(payload->'source_nodes',payload->'params'->'source_nodes','[]'))>0;
