create table dream_summary (
 user_id text not null, kind text not null check(kind in ('anchor','object')),
 key text not null, version integer not null, payload jsonb not null,
 watermarks jsonb not null default '{}', fingerprint text not null,
 dirty boolean not null default false, updated_at timestamptz not null,
 primary key(user_id,kind,key)
);
create table dream_source (
 user_id text not null, kind text not null, key text not null, version integer not null,
 source_id text not null, primary key(user_id,kind,key,version,source_id)
);
create index dream_source_reverse on dream_source(user_id,source_id);
create index dream_summary_pending on dream_summary(user_id,kind) where dirty;
create table dream_pending (
 user_id text not null, session_id text not null, revision bigint not null default 1,
 processed bigint not null default 0, updated_at timestamptz not null default now(),
 primary key(user_id,session_id),
 foreign key(session_id) references assistant_session(session_id) on delete cascade
);
create function dream_session_dirty() returns trigger language plpgsql as $$
declare uid text;
begin
 if new.type='wrapup_failed' then
   update dream_pending set updated_at=now() where session_id=new.session_id;
   return new;
 end if;
 if new.type not in ('assistant','rating','compaction','consolidation') then return new; end if;
 select user_id into uid from assistant_session where session_id=new.session_id;
 insert into dream_pending(user_id,session_id) values(uid,new.session_id)
 on conflict(user_id,session_id) do update set revision=dream_pending.revision+1,updated_at=now();
 update dream_summary set dirty=true where user_id=uid and (kind,key) in
 (select kind,key from dream_source where user_id=uid and source_id='session:'||new.session_id);
 return new;
end $$;
create trigger dream_session_dirty after insert on assistant_entry
for each row execute function dream_session_dirty();
insert into dream_pending(user_id,session_id)
select s.user_id,s.session_id from assistant_session s where exists
(select 1 from assistant_entry e where e.session_id=s.session_id and e.type='assistant');

create function dream_item_dirty() returns trigger language plpgsql as $$
begin
 update dream_summary set dirty=true where user_id=old.user_id and (kind,key) in
 (select kind,key from dream_source where user_id=old.user_id and source_id='item:'||old.item_id);
 update dream_pending set revision=revision+1,processed=0 where user_id=old.user_id and session_id in
 (select substring(s.source_id from 9) from dream_source s join dream_source d
 using(user_id,kind,key,version) where d.user_id=old.user_id
 and d.source_id='item:'||old.item_id and s.source_id like 'session:%');
 return old;
end $$;
create trigger dream_item_dirty after update or delete on memory_item
for each row execute function dream_item_dirty();
