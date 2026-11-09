import hashlib
import random

RANDOM_KEY_SCHEME_VERSION = "crn-typed-v1"
_NAMESPACE_SEPARATOR = "|ns|"


def _digest(seed: int, key: tuple[object, ...]) -> bytes:
    text = "|".join([str(seed), *(repr(k) for k in key)])
    return hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()


class CRN:
    """Common random numbers: every draw is addressed by a semantic key, never by call order.

    The same (seed, namespace, key) always yields the same value, whatever other draws happened
    before. Experimental conditions that reuse a learner seed therefore share the randomness of
    every decision they have in common (spec §10.2).

    The digest key is (*namespace, "|ns|", *key) for a non-empty namespace, so a namespaced draw
    never collides with an untyped draw whose key begins with the namespace; an empty namespace
    uses the bare key and is byte-identical to the untyped scheme. Learners use namespace (learner_type,), so their draws are keyed by
    (learner_type, seed, stream, event_key) (spec §4.3): learners of different types that share
    a seed do not share draws, while the same type and seed keep common random numbers across
    conditions.
    """

    def __init__(self, seed: int, namespace: tuple[str, ...] = ()):
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise TypeError("seed must be an int")
        if not isinstance(namespace, tuple) or not all(isinstance(n, str) for n in namespace):
            raise TypeError("namespace must be a tuple of str")
        self.seed = seed
        self.namespace = namespace

    def _key(self, key: tuple[object, ...]) -> tuple[object, ...]:
        return (*self.namespace, _NAMESPACE_SEPARATOR, *key) if self.namespace else key

    def uniform(self, *key: object) -> float:
        """A float in [0, 1) determined only by (seed, namespace, key)."""
        n = int.from_bytes(_digest(self.seed, self._key(key))[:8], "big")
        return (n >> 11) * (1.0 / (1 << 53))

    def bernoulli(self, p: float, *key: object) -> bool:
        return self.uniform(*key) < p

    def rng(self, *key: object) -> random.Random:
        """A private random.Random stream determined only by (seed, namespace, key)."""
        return random.Random(int.from_bytes(_digest(self.seed, self._key(key)), "big"))
