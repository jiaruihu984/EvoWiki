create function dream_object_dirty() returns trigger language plpgsql as $$
declare rid text; cid text;
begin
 perform pg_advisory_xact_lock(hashtext('memory:user:'||new.user_id));
 if tg_table_name='harness_round' then rid := new.round_id;
 elsif tg_table_name='harness_change' then rid := new.round_id; cid := new.change_id;
 elsif tg_table_name='harness_experience' then
   rid := new.evidence->>'round_id'; cid := new.evidence->>'change_id';
 elsif tg_table_name in ('harness_observation','harness_fast_check') then
   cid := new.change_id;
   select round_id into rid from harness_change where change_id=cid;
 else rid := new.round_id;
 end if;
 update dream_summary set dirty=true where user_id=new.user_id and kind='object'
 and key in (select key from dream_source where user_id=new.user_id and kind='object'
             and source_id in ('round:'||rid,'change:'||cid));
 return new;
end $$;
create trigger dream_object_round after insert or update on harness_round
for each row execute function dream_object_dirty();
create trigger dream_object_change after insert or update on harness_change
for each row execute function dream_object_dirty();
create trigger dream_object_experience after insert on harness_experience
for each row execute function dream_object_dirty();
create trigger dream_object_observation after insert on harness_observation
for each row execute function dream_object_dirty();
create trigger dream_object_gate after insert on harness_gate
for each row execute function dream_object_dirty();
create trigger dream_object_fast_check after insert on harness_fast_check
for each row execute function dream_object_dirty();
