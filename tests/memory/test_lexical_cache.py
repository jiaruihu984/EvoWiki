from evowiki.memory import lexical


def test_same_corpus_reuses_index_but_changes_and_forgetting_rebuild(monkeypatch):
    lexical.invalidate('cache-user')
    builds = []
    original = lexical._index

    def build(tokens):
        builds.append(tokens)
        return original(tokens)

    monkeypatch.setattr(lexical, '_index', build)
    docs = [('old', '稀有问题：中文分词'), ('other', '无关数学记录')]
    assert lexical.rank('cache-user', 'experience', docs, '中文分词', k=5)[0][0] == 'old'
    lexical.rank('cache-user', 'experience', docs, '数学', k=5)
    assert len(builds) == 1
    assert not lexical.rank('cache-user', 'experience', docs[1:], '中文分词', k=5)
    assert len(builds) == 2
    lexical.invalidate('cache-user')
    lexical.rank('cache-user', 'experience', docs[1:], '数学', k=5)
    assert len(builds) == 3


def test_users_do_not_share_a_cached_index(monkeypatch):
    lexical.invalidate('cache-a')
    lexical.invalidate('cache-b')
    docs = [('same-id', 'private evidence')]
    builds = []
    original = lexical._index

    def build(tokens):
        builds.append(tokens)
        return original(tokens)

    monkeypatch.setattr(lexical, '_index', build)
    lexical.rank('cache-a', 'anchor', docs, 'private', k=1)
    lexical.rank('cache-b', 'anchor', docs, 'private', k=1)
    assert len(builds) == 2
