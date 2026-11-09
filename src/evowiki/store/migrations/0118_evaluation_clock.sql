create table evaluation_record_clock (
 user_id text not null,
 relation text not null,
 record_id text not null,
 recorded_at timestamptz not null,
 version bigint not null default 1,
 primary key(user_id,relation,record_id)
);
create index evaluation_record_clock_cutoff on evaluation_record_clock(user_id,relation,recorded_at);

create function evaluation_stamp_record() returns trigger language plpgsql as $$
declare value jsonb; uid text; identity text;
begin
 value := case when tg_op='DELETE' then to_jsonb(old) else to_jsonb(new) end;
 uid := value->>'user_id';
 if uid is null then return null; end if;
 identity := value->>tg_argv[0];
 if tg_nargs=2 then identity := identity || ':' || (value->>tg_argv[1]); end if;
 if tg_op='DELETE' then
   delete from evaluation_record_clock where user_id=uid and relation=tg_table_name and record_id=identity;
 else
   insert into evaluation_record_clock values(uid,tg_table_name,identity,clock_timestamp(),1)
   on conflict(user_id,relation,record_id) do update
      set recorded_at=excluded.recorded_at,version=evaluation_record_clock.version+1;
 end if;
 return null;
end $$;

create trigger evaluation_clock after insert or update or delete on harness_round
 for each row execute function evaluation_stamp_record('round_id');
create trigger evaluation_clock after insert or update or delete on harness_change
 for each row execute function evaluation_stamp_record('change_id');
create trigger evaluation_clock after insert or update or delete on harness_experience
 for each row execute function evaluation_stamp_record('exp_id');
create trigger evaluation_clock after insert or update or delete on harness_observation
 for each row execute function evaluation_stamp_record('obs_id');
create trigger evaluation_clock after insert or update or delete on harness_gate
 for each row execute function evaluation_stamp_record('gate_id');
create trigger evaluation_clock after insert or update or delete on forge_candidate
 for each row execute function evaluation_stamp_record('id');
create trigger evaluation_clock after insert or update or delete on forge_patch
 for each row execute function evaluation_stamp_record('candidate_id');
create trigger evaluation_clock after insert or update or delete on harness_patch_observation
 for each row execute function evaluation_stamp_record('obs_id');
create trigger evaluation_clock after insert or update or delete on harness_fast_check
 for each row execute function evaluation_stamp_record('check_id');
create trigger evaluation_clock after insert or update or delete on agent_run
 for each row execute function evaluation_stamp_record('run_id');
create trigger evaluation_clock after insert or update or delete on dream_summary
 for each row execute function evaluation_stamp_record('kind','key');

-- Observe existing versions now, without pretending their logical event dates prove visibility.
do $$
declare tbl text; id_column text;
begin
 for tbl,id_column in select * from (values
   ('harness_round','round_id'), ('harness_change','change_id'),
   ('harness_experience','exp_id'), ('harness_observation','obs_id'),
   ('harness_gate','gate_id'), ('forge_candidate','id'), ('forge_patch','candidate_id'),
   ('harness_patch_observation','obs_id'), ('harness_fast_check','check_id'),
   ('agent_run','run_id')) as sources(relation,identity_column)
 loop
   execute format('insert into evaluation_record_clock '
     'select user_id,%L,%I,clock_timestamp(),1 from %I where user_id is not null '
     'on conflict do nothing',tbl,id_column,tbl);
 end loop;
 insert into evaluation_record_clock
   select user_id,'dream_summary',kind || ':' || key,clock_timestamp(),1 from dream_summary
   on conflict do nothing;
end $$;
