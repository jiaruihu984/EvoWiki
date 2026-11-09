alter table rsi_feedback add column reviewed_worthwhile boolean;
update rsi_feedback set reviewed_worthwhile=true where reviewed_fingerprint is not null;
