# External Engagements

PRs filed against external repos using pact-detected violations. Tracks what we found, where we filed, and what happened.

| Date | Repo | PR | Mode | Stars | Status | Notes |
|------|------|----|------|-------|--------|-------|
| 2026-05-14 | [Arize-ai/phoenix](https://github.com/Arize-ai/phoenix) | [#13237](https://github.com/Arize-ai/phoenix/pull/13237) | `llm_response_unguarded` | 4.5k | **Merged** | First external merge; merged by @RogerHYang same day |
| 2026-05-14 | [comet-ml/opik](https://github.com/comet-ml/opik) | [#6700](https://github.com/comet-ml/opik/pull/6700) | `llm_response_unguarded` | 5k | Open | Bot requested streaming fix + test; streaming guard applied; replied explaining docs scripts have no test runner |
| 2026-05-14 | [vibrantlabsai/ragas](https://github.com/vibrantlabsai/ragas) | [#2720](https://github.com/vibrantlabsai/ragas/pull/2720) | `llm_response_unguarded` | 8k | Open | 0 reviews |
| 2026-05-14 | [microsoft/markitdown](https://github.com/microsoft/markitdown) | [#1876](https://github.com/microsoft/markitdown/pull/1876) | `llm_response_unguarded` | 52k | Open | 0 reviews; highest-star repo targeted so far |
| 2026-05-15 | [celery/django-celery-beat](https://github.com/celery/django-celery-beat) | [#1038](https://github.com/celery/django-celery-beat/pull/1038) | `save_without_update_fields` | 1.5k | Open | CI failed: `no_changes` sentinel passed as `update_fields` — not a DB column; fixed (f1055f2), CI awaiting maintainer approval |
| 2026-05-16 | [celery/django-celery](https://github.com/celery/django-celery) | [#643](https://github.com/celery/django-celery/pull/643) | `save_without_update_fields` | 1.6k | Open | Same race condition as django-celery-beat; found via r16-7 celery corpus batch |

## Findings that drove ADRs

| Repo | Finding | ADR |
|------|---------|-----|
| jina-ai/serve | `except: raise` flagged as bare_except — pure re-raise swallows nothing | [ADR-006](adr/ADR-006-bare-except-reraise-exclusion.md) |
| arize-ai/phoenix | `response.choices[0]` unguarded — `llm_response_unguarded` mode confirmed real | (mode existed; PR filed) |

## Corpus repos with notable findings

Repos from the scan corpus that surfaced interesting patterns (not PRs, but learning):

| Repo | Violations | Pattern | Verdict |
|------|-----------|---------|---------|
| jina-ai/serve | 24 bare_except | `except: raise` routing | FP → fixed ADR-006 |
| Cloud-CV/EvalAI | 45 save_without_update_fields | Django eval platform with concurrent writes | Real violations |
| celery/kombu | 6 bare_except | Cleanup silencing + context manager protocol | Real violations |
| wagtail/wagtail | 7 save_without_update_fields | Production Django CMS | Real violations |
| django/django | 2 bare_except | App-discovery probe pattern | Intentional, real |
