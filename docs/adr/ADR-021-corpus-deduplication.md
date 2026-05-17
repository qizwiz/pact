# ADR-021: Corpus deduplication — query overlap causes inflated violation counts

## Status

Accepted

## Context

`scan_github` queries GitHub by keyword (e.g., `llm stars:>500`, `openai stars:>300`, `agent stars:>400`). A large agent framework like `NousResearch/hermes-agent` (154k stars) matches all three queries and is scanned three times, producing three copies of its violations in `corpus.jsonl`.

When the raw corpus reached 59,688 lines, deduplication revealed 46,507 duplicates (77.9%). True unique violations: **13,181** across **703 unique repositories**.

The README and pitch materials cited "59k+ violations" — a 4.5x overcount. Reporting inflated numbers undermines the credibility of the tool.

## Decision

**Deduplicate `corpus.jsonl` after every append** using `(repo, file, line, mode)` as the unique key. On each batch completion:

```python
seen = set()
kept = []
with open(corpus_path) as f:
    for line in f:
        d = json.loads(line)
        key = (d["repo"], d["file"], d["line"], d["mode"])
        if key not in seen:
            seen.add(key)
            kept.append(line)
with open(corpus_path, "w") as f:
    f.writelines(kept)
```

Report **unique violation count**, not raw line count, in README and corpus statistics.

## Why not prevent duplicates at scan time?

`scan_github` could check a seen-repos set and skip already-scanned repos. This is the more elegant solution but requires state persistence across scan processes. The dedup-on-append approach is simpler, stateless, and self-healing — any existing inflated corpus is corrected on the next append.

A future improvement: write a `--seen-repos` file that scan_github checks before scanning a repo.

## Consequences

- Corpus stat corrected: **13,181 unique violations / 703 repos** (not 59k)
- README updated: "13k+ unique violations across 700+ real repositories"
- Every future batch append must be followed by deduplication before updating corpus stats
- The raw batch `.jsonl` files are NOT deduplicated — only `corpus.jsonl` is the deduplicated aggregate

## Related

- [ADR-009](ADR-009-monolith-density-signal.md) — per-file density signal; accurate per-repo counts depend on dedup
