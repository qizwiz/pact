# External Engagements

PRs filed against external repos using pact-detected violations. Tracks what we found, where we filed, and what happened.

| Date | Repo | PR | Mode | Stars | Status | Notes |
|------|------|----|------|-------|--------|-------|
| 2026-05-14 | [Arize-ai/phoenix](https://github.com/Arize-ai/phoenix) | [#13237](https://github.com/Arize-ai/phoenix/pull/13237) | `llm_response_unguarded` | 4.5k | **Merged** | First external merge; merged by @RogerHYang same day |
| 2026-05-14 | [comet-ml/opik](https://github.com/comet-ml/opik) | [#6700](https://github.com/comet-ml/opik/pull/6700) | `llm_response_unguarded` | 5k | Open | Bot requested streaming fix + test; streaming guard applied; replied explaining docs scripts have no test runner |
| 2026-05-14 | [vibrantlabsai/ragas](https://github.com/vibrantlabsai/ragas) | [#2720](https://github.com/vibrantlabsai/ragas/pull/2720) | `llm_response_unguarded` | 8k | Open | 0 reviews |
| 2026-05-14 | [microsoft/markitdown](https://github.com/microsoft/markitdown) | [#1876](https://github.com/microsoft/markitdown/pull/1876) | `llm_response_unguarded` | 52k | Open | 0 reviews; highest-star repo targeted so far |
| 2026-05-15 | [celery/django-celery-beat](https://github.com/celery/django-celery-beat) | [#1038](https://github.com/celery/django-celery-beat/pull/1038) | `save_without_update_fields` | 1.5k | Open | Maintainer requested tests; added 3 regression tests + pk guard fix (c372f43); replied to auvipy |
| 2026-05-16 | [celery/django-celery](https://github.com/celery/django-celery) | [#643](https://github.com/celery/django-celery/pull/643) | `save_without_update_fields` | 1.6k | Open | Same race condition as django-celery-beat; found via r16-7 celery corpus batch |

## Findings that drove ADRs

| Repo | Finding | ADR |
|------|---------|-----|
| jina-ai/serve | `except: raise` flagged as bare_except — pure re-raise swallows nothing | [ADR-006](adr/ADR-006-bare-except-reraise-exclusion.md) |
| arize-ai/phoenix | `response.choices[0]` unguarded — `llm_response_unguarded` mode confirmed real | (mode existed; PR filed) |

## Findings ready to file (pending Codeberg account)

| Repo | Branch | Violations | Notes |
|------|--------|-----------|-------|
| [pennersr/django-allauth](https://codeberg.org/allauth/django-allauth) | qizwiz/django-allauth:fix/save-update-fields | 3 `save_without_update_fields` | Canonical repo is Codeberg, not GitHub. Code tested (231 passed). Fixes: adapter.py set_password(), models.py set_as_primary() (pk guard needed for by-code flow), EmailConfirmation.send(). Needs Codeberg fork to file PR. |

## Corpus repos with notable findings

Repos from the scan corpus that surfaced interesting patterns (not PRs, but learning):

| Repo | Violations | Pattern | Verdict |
|------|-----------|---------|---------|
| jina-ai/serve | 24 bare_except | `except: raise` routing | FP → fixed ADR-006 |
| Cloud-CV/EvalAI | 45 save_without_update_fields | Django eval platform, 16 in aws_utils.py (worker paths), 11 in views.py | **PR candidate** — 2k stars |
| celery/kombu | 6 bare_except | Cleanup silencing + context manager protocol | Real violations |
| wagtail/wagtail | 7 save_without_update_fields | Production Django CMS | Real violations |
| django/django | 2 bare_except | App-discovery probe pattern | Intentional, real |
| paperless-ngx/paperless-ngx | 12 save_without_update_fields | Document management, async worker paths | **PR candidate** — 40k stars |
| pennersr/django-allauth | 8 save_without_update_fields | Auth library, user.save() after set_password — security-relevant | **PR candidate** — 10k stars |
| LibrePhotos/librephotos | 95 save_without_update_fields | Photo management; 33 in user.py alone | **PR candidate** — 8k stars |
| healthchecks/healthchecks | 31 save_without_update_fields | Monitoring service with concurrent pings | **PR candidate** — 10k stars |
| django-cms/django-cms | 13 save_without_update_fields | Django CMS; mixed with optional_dereference (18 total) | **PR candidate** — 10k stars |
| suitenumerique/docs | 9 save_without_update_fields | French gov docs platform | **PR candidate** — new find r18-8 |
| wagtail/wagtail | 7 save_without_update_fields | Production Django CMS, confirmed real | **PR candidate** — 18k stars |
