"""
pact.django_guard — runtime enforcement for save_without_update_fields.

Decorates Django views or model methods to enforce that Model.save()
is always called with update_fields when only a subset of fields changed.

In development: raises PactViolation on the first unscoped save().
In production (PACT_ENFORCE=0): logs a warning, never raises.

Usage:

    from pact.django_guard import save_scoped

    # Enforce that only "name" can be saved from this view
    @save_scoped("name")
    def project_settings(request, code):
        project.name = form.cleaned_data["name"]
        project.save()   # PactViolation if update_fields != {"name"}

    # Enforce that ANY save must declare update_fields
    @save_scoped()
    def update_api_key(request, code):
        project.api_key = project.set_api_key()
        project.save(update_fields=["api_key"])   # fine
"""

from __future__ import annotations

import functools
import logging
import os
from contextlib import contextmanager
from typing import Optional
from unittest.mock import patch

logger = logging.getLogger("pact")

_ENFORCE = os.environ.get("PACT_ENFORCE", "1") != "0"


class PactViolation(Exception):
    """Raised when a save_without_update_fields constraint is violated at runtime."""


def _make_guarded_save(allowed_fields: Optional[frozenset[str]], original_save):
    """Return a patched save() that enforces update_fields."""

    @functools.wraps(original_save)
    def guarded_save(instance, *args, **kwargs):
        update_fields = kwargs.get("update_fields") or (
            args[0] if args and isinstance(args[0], (list, tuple, frozenset, set)) else None
        )

        if update_fields is None:
            msg = (
                f"pact: {type(instance).__name__}.save() called without update_fields. "
                f"This is a save_without_update_fields violation — a full save overwrites "
                f"all fields, risking silent data loss under concurrent writes. "
                f"Add update_fields=[...] to scope the save."
            )
            if _ENFORCE:
                raise PactViolation(msg)
            else:
                logger.warning(msg)

        elif allowed_fields and not frozenset(update_fields).issubset(allowed_fields):
            unexpected = frozenset(update_fields) - allowed_fields
            msg = (
                f"pact: {type(instance).__name__}.save(update_fields={list(update_fields)}) "
                f"saves fields {unexpected} not declared in @save_scoped({sorted(allowed_fields)}). "
                f"Declare all modified fields in the decorator."
            )
            if _ENFORCE:
                raise PactViolation(msg)
            else:
                logger.warning(msg)

        return original_save(instance, *args, **kwargs)

    return guarded_save


@contextmanager
def save_scoped_ctx(*fields: str):
    """Context manager version of save_scoped."""
    try:
        from django.db.models import Model
    except ImportError:
        yield
        return

    allowed = frozenset(fields) if fields else None
    original = Model.save
    guarded = _make_guarded_save(allowed, original)

    with patch.object(Model, "save", guarded):
        yield


def save_scoped(*fields: str):
    """
    Decorator that enforces update_fields on every Model.save() call
    within the decorated function.

    @save_scoped("name")              — only "name" may be saved
    @save_scoped("api_key")           — only "api_key" may be saved
    @save_scoped()                    — any save must declare update_fields
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with save_scoped_ctx(*fields):
                return func(*args, **kwargs)
        return wrapper
    return decorator
