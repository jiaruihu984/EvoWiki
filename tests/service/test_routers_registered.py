"""Every src/evowiki/service/routes_*.py router must be mounted by create_app."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import evowiki.service
from evowiki.service.app import create_app


def _route_keys(routes) -> set[tuple[str, frozenset[str]]]:
    return {(r.path, frozenset(getattr(r, "methods", None) or ())) for r in routes if hasattr(r, "path")}


def _mounted(app) -> tuple[list, set]:
    """Newer FastAPI wraps include_router results in `_IncludedRouter` (keeps `original_router`)."""
    originals = [getattr(r, "original_router", None) for r in app.routes]
    return [o for o in originals if o is not None], _route_keys(app.routes)


def test_every_routes_module_is_registered():
    pkg_dir = Path(evowiki.service.__file__).parent
    names = sorted(p.stem for p in pkg_dir.glob("routes_*.py"))
    assert names, "no routes_*.py modules found"
    originals, app_routes = _mounted(create_app(SimpleNamespace()))
    for name in names:
        module = importlib.import_module(f"evowiki.service.{name}")
        router = getattr(module, "router", None)
        assert router is not None, f"{name} has no `router`"
        if any(router is o for o in originals):
            continue
        missing = _route_keys(router.routes) - app_routes
        assert not missing, f"{name} is not registered in create_app: {sorted(map(str, missing))}"
