"""Feature routers.

`create_app` calls `register_routers(app)` once, after middleware and before
the static frontend mount, so feature areas can be added here without editing
`main.py`.
"""

from __future__ import annotations

from fastapi import FastAPI


def register_routers(app: FastAPI) -> None:
    """Attach every feature router to the application."""
    # Routers are imported lazily so an import error in one feature area names
    # itself instead of taking down `arranger_api.main` at import time.
    from importlib import import_module

    for module_name in _ROUTER_MODULES:
        module = import_module(f"{__name__}.{module_name}")
        app.include_router(module.router)


# Populated as feature routers land. Order is registration order.
_ROUTER_MODULES: tuple[str, ...] = ("catalog", "projects", "account", "jobs")
