from evowiki.harness.experience import comparison_batches, dump


def test_large_dossiers_compare_every_original_fragment_with_a_different_attempt():
    records = [{'id': f'change:{i}', 'round_id': f'r{i}',
                'sources': [f'change:{i}', f'session:s{i}'],
                'observations': ['带引号的原文 "x"\\\n' * 6000]} for i in range(2)]
    batches = list(comparison_batches(records))
    assert batches and all(len(dump(batch)) <= 24000 for batch in batches)
    assert all(len({record['round_id'] for record in batch}) == 2 for batch in batches)
    for record in records:
        pieces = {part['excerpt_start']: part['original_excerpt']
                  for batch in batches for part in batch
                  if part['id'] == record['id']}
        assert ''.join(pieces[start] for start in sorted(pieces)) == dump(record)
    assert all(part['sources'] == [part['id']] for batch in batches for part in batch)
