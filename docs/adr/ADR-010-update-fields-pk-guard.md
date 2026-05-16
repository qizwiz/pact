# ADR-010: `update_fields` fixes require a `pk` guard on methods callable from unsaved instances

## Status

Accepted

## Context

When pact detects a `save_without_update_fields` violation and we apply the fix — replacing `.save()` with `.save(update_fields=[...])` — Django raises:

```
ValueError: Cannot force an update in save() with no primary key.
```

if the method is called on an object that has never been written to the database (no PK assigned). This is because `update_fields` instructs Django to force an `UPDATE` statement; without a PK, no row can be targeted.

This was discovered in two consecutive PRs:

- **celery/django-celery-beat#1038**: `ModelEntry._disable()` and `is_due()` — unit tests in `t/unit/test_schedulers.py` call these methods on `PeriodicTask` instances constructed via `Model(**kwargs)` (no `.save()`). CI failed with `ValueError` after our `update_fields` fix (commit `f1055f2`). Fixed in `c372f43`.

- **pennersr/django-allauth** (qizwiz/django-allauth:fix/save-update-fields): `EmailAddress.set_as_primary()` — the `email_verification_by_code` flow constructs in-memory `EmailAddress` instances without PKs. Test `test_add_or_change_email[True]` failed with the same `ValueError`.

In production, these code paths always operate on DB-persisted objects. The no-PK case is a unit-test artifact, but it's pervasive: test suites commonly build model instances with factory functions that don't persist them, then call the methods under test directly.

## Decision

When applying a `save_without_update_fields` fix to a method that can be called on an unsaved instance, add a `pk` guard:

```python
# BEFORE (fix attempt — breaks on unsaved instances):
self.save(update_fields=['field_a', 'field_b'])

# AFTER (correct fix):
if self.pk:
    self.save(update_fields=['field_a', 'field_b'])
else:
    self.save()
```

The guard is only required when the method is reachable from a construction path that does not guarantee a prior `.save()`. For methods that are only ever called on DB-fetched objects (e.g., `ModelEntry.save()` in schedulers.py, which fetches via `get(pk=self.model.pk)`) the guard is unnecessary.

When writing regression tests for `update_fields` fixes, set `obj.pk = 1` (or equivalent) on mock instances before patching `save`, so the test exercises the `update_fields` path rather than the fallback:

```python
m = create_model_interval(...)
m.pk = 1  # simulate a DB-persisted instance
with patch.object(m, 'save') as mock_save:
    e._disable(m)
mock_save.assert_called_once_with(update_fields=['enabled'])
```

## Consequences

- Every `save_without_update_fields` fix must include a callsite analysis: is this method called on potentially unsaved objects?
- CI is the authoritative check — if `update_fields` was applied incorrectly, the test suite will surface it as a `ValueError`.
- The five-layer verification process (TLA+ → ADR → Z3 → Hypothesis → integration probe) must include running the full test suite of the target repo before filing a PR. This is already required by the memory rule, but this ADR makes the reason concrete.
