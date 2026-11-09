"""Bounded per-user BM25 indexes; callers filter source visibility before ranking."""

import hashlib
import threading
from collections import OrderedDict
from collections.abc import Sequence

from evowiki.sources.search import _index, tokenize

_lock = threading.Lock()
_cache: OrderedDict[tuple[str, str], tuple[str, object]] = OrderedDict()


def invalidate(user_id: str) -> None:
    with _lock:
        for key in list(_cache):
            if key[0] == user_id:
                del _cache[key]


def rank(user_id: str, scope: str, docs: Sequence[tuple[str, str]], query: str,
         *, k: int) -> list[tuple[str, float]]:
    words = tokenize(query)
    if not docs or not words or k <= 0:
        return []
    digest = hashlib.sha256()
    for identity, text in docs:
        for value in (identity, text):
            raw = value.encode('utf-8')
            digest.update(len(raw).to_bytes(8, 'big'))
            digest.update(raw)
    fingerprint = digest.hexdigest()
    key = (user_id, scope)
    with _lock:
        cached = _cache.get(key)
        if cached is None or cached[0] != fingerprint:
            index = _index([tokenize(text) for _, text in docs])
            _cache[key] = (fingerprint, index)
        else:
            index = cached[1]
        _cache.move_to_end(key)
        while len(_cache) > 32:
            _cache.popitem(last=False)
        if index is None:
            return []
        scores = index.get_scores(words)
        order = sorted((i for i in range(len(docs)) if scores[i] > 0),
                       key=lambda i: (-float(scores[i]), docs[i][0]))
        return [(docs[i][0], float(scores[i])) for i in order[:k]]
