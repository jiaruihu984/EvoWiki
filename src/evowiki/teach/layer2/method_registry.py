"""Method registry of the layer-2 entries (RSI design §2): versioned A1 FitModel (kind "A2") per
user. Same rules as `teach.registry`:

- A user with no rows is on an implicit v1: `DEFAULT_FIT_MODEL`. `current_*` return it without
  writing. The implicit v1 is always the current default: since Task 20 its
  FitModel fits 7 paths (start_known joined DEFAULT_FIT_PATHS) under the same version number,
  while a stored v1 row (written by a user's first commit / rollback) keeps the paths it stored.
  The two are told apart by `FitModel.defaults_id`: the implicit default says `FIT_DEFAULTS_ID`;
  a v1 row stored before the field existed has None (the 6-path defaults).
- The first `commit` / `rollback` writes that v1 row first (so the chain is complete and a rollback
  to 1 restores the default), then appends last + 1. The carried model's own `version` is set to
  the row's.
- Rollback never deletes: it appends a new version that copies an older one's content
  (change_id None).

The v1 write and the vN write run in the store's transaction (`layer2.store.transaction`; a store
without one: if the second fails, v1 stays — harmless, it is the user's current method). A caller
that also writes the change row wraps both in the same transaction (nested = savepoint)."""

from collections.abc import Sequence
from datetime import UTC, datetime

from evowiki.teach.contracts import (
    DEFAULT_FIT_PATHS,
    FitModel,
    MethodChange,
    MethodKind,
    MethodVersion,
)
from evowiki.teach.layer2.store import Layer2Store, transaction

FIT_DEFAULTS_ID = "fit-defaults@2"      # @1 (no id stored) = the 6 paths before start_known
DEFAULT_FIT_MODEL = FitModel(version=1, paths=DEFAULT_FIT_PATHS, defaults_id=FIT_DEFAULTS_ID)
_IMPLICIT_AT = datetime(1970, 1, 1, tzinfo=UTC)


def _implicit_v1(kind: MethodKind, at: datetime) -> MethodVersion:
    return MethodVersion(kind=kind, version=1, at=at, fit_model=DEFAULT_FIT_MODEL)


def current(store: Layer2Store, user_id: str, kind: MethodKind) -> MethodVersion:
    rows = store.method_versions(user_id, kind)
    return rows[-1] if rows else _implicit_v1(kind, _IMPLICIT_AT)


def current_model(store: Layer2Store, user_id: str) -> FitModel:
    return current(store, user_id, "A2").fit_model


def _append(store: Layer2Store, user_id: str, kind: MethodKind, *, fit_model: FitModel,
            change_id: str | None, now: datetime) -> MethodVersion:
    with transaction(store):
        rows = store.method_versions(user_id, kind)
        if not rows:
            store.add_method_version(user_id, _implicit_v1(kind, now))
            last = 1
        else:
            last = rows[-1].version
        new = last + 1
        v = MethodVersion(
            kind=kind, version=new, at=now, change_id=change_id,
            fit_model=fit_model.model_copy(update={"version": new}))
        store.add_method_version(user_id, v)
    return v


def commit(store: Layer2Store, user_id: str, kind: MethodKind, *, fit_model: FitModel,
           change_id: str | None, now: datetime) -> MethodVersion:
    return _append(store, user_id, kind, fit_model=fit_model, change_id=change_id, now=now)


def rollback(store: Layer2Store, user_id: str, kind: MethodKind, version: int, *,
             now: datetime) -> MethodVersion:
    rows = store.method_versions(user_id, kind) or [_implicit_v1(kind, _IMPLICIT_AT)]
    target = next((r for r in rows if r.version == version), None)
    if target is None:
        raise ValueError(f"{user_id}/{kind}: no version {version}")
    return _append(store, user_id, kind, fit_model=target.fit_model, change_id=None, now=now)


def latest_applied(changes: Sequence[MethodChange], kind: str) -> str | None:
    """The change id of the latest applied method change of `kind` (highest to_version, then the
    newest), or None. The only change of its kind that can be undone or rolled back: rolling back an
    older one would copy a whole version and silently drop the later changes."""
    applied = [c for c in changes if c.kind == kind and c.status == "applied"]
    top = max(applied, key=lambda c: (c.to_version or 0, c.created_at), default=None)
    return None if top is None else top.change_id
