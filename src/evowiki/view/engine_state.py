"""Phase 0: the slow-loop engine's mutable state as a serializable value (live V persistence).

`ViewEngine` keeps four pieces of state across passes: the N = 2 hysteresis streaks (R1), the
last slow-loop session index, and the R1.1 counterfactual cap reservations (slots reserved per
session; withheld (concept, protected_since, value) triples). `EngineState` holds them in a
canonical order so equal states serialize identically."""

import json
from collections.abc import Hashable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from evowiki.view.roles import Role

SCHEMA = "engine-state/1"
Slot = tuple[str, ...]


def encode_value(slot: Slot, value: Hashable) -> Any:
    match slot[0]:
        case "SET_ROLE":
            role, anchor = value  # type: ignore[misc]
            return [role.value, anchor]
        case "SET_PERSPECTIVE":
            return value
        case "CREATE_HUB":
            return list(value)  # type: ignore[call-overload]
    raise ValueError(f"unknown slot kind {slot[0]!r}")


def decode_value(slot: Slot, obj: Any) -> Hashable:
    match slot[0]:
        case "SET_ROLE":
            return (Role(obj[0]), obj[1])
        case "SET_PERSPECTIVE":
            return str(obj)
        case "CREATE_HUB":
            return tuple(obj)
    raise ValueError(f"unknown slot kind {slot[0]!r}")


def _key(slot: Slot, value: Hashable) -> str:
    return json.dumps([list(slot), encode_value(slot, value)], sort_keys=True)


@dataclass(frozen=True)
class EngineState:
    """`streaks`: (slot, value, last session index, streak) sorted by key; `reserved`:
    (session_id, n) sorted; `withheld`: (concept, protected_since, value) sorted. Withheld
    values are always SetRole values (R1.1 withholds SetRole winners only)."""

    streaks: tuple[tuple[Slot, Hashable, int, int], ...] = ()
    last_session_index: int | None = None
    reserved: tuple[tuple[str, int], ...] = ()
    withheld: tuple[tuple[str, datetime, Hashable], ...] = ()

    @classmethod
    def of(cls, streaks, last_session_index, reserved, withheld) -> "EngineState":
        return cls(
            streaks=tuple(sorted(streaks, key=lambda t: _key(t[0], t[1]))),
            last_session_index=last_session_index,
            reserved=tuple(sorted((s, n) for s, n in reserved if n)),
            withheld=tuple(sorted(withheld, key=lambda t: (t[0], t[1].isoformat(),
                                                            _key(("SET_ROLE", t[0]), t[2])))),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "streaks": [[list(s), encode_value(s, v), last, n] for s, v, last, n in self.streaks],
            "last_session_index": self.last_session_index,
            "reserved": [[s, n] for s, n in self.reserved],
            "withheld": [[c, t.isoformat(), encode_value(("SET_ROLE", c), v)]
                         for c, t, v in self.withheld],
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> "EngineState":
        if d.get("schema") != SCHEMA:
            raise ValueError(f"engine state schema {d.get('schema')!r} != {SCHEMA}")
        streaks = [(tuple(s), decode_value(tuple(s), v), int(last), int(n))
                   for s, v, last, n in d["streaks"]]
        withheld = [(c, datetime.fromisoformat(t), decode_value(("SET_ROLE", c), v))
                    for c, t, v in d["withheld"]]
        return cls.of(streaks, d["last_session_index"], [(s, int(n)) for s, n in d["reserved"]],
                      withheld)


EMPTY_ENGINE_STATE = EngineState()
