alter table wiki_asset drop constraint wiki_asset_kind_check;
alter table wiki_asset add constraint wiki_asset_kind_check check (kind in ('html', 'evoui', 'image'));
