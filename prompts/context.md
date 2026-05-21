# Git Context Extractor

You are reading the commit history, changelog, and inline notes for a source file.
Your job is to extract **confirmed violation signals** — things that actually broke,
for real users, that maintainers cared enough to fix or flag.

## File: {{file_path}}

## Git Log (fix/edge-case commits touching this file)
```
{{git_log}}
```

## Changelog excerpts mentioning this file or its functions
```
{{changelog}}
```

## TODO / FIXME / HACK / XXX comments in source
```
{{inline_notes}}
```

## PART 1: THINK (do not output this)

For each signal above, ask:
- What invariant was violated?
- What input or state triggered it?
- Is there a pattern — does the same area keep breaking?

## PART 2: OUTPUT — one JSON object, start with {, end with }, nothing else

```
{
  "file": "{{file_path}}",
  "confirmed_violations": [
    {
      "source": "git | changelog | comment",
      "commit_or_line": "abc1234 or line 42",
      "function": "function name if identifiable",
      "what_broke": "one sentence describing the violation",
      "trigger": "the input or condition that caused it",
      "invariant": "formal statement of what should always hold",
      "severity": "critical | high | medium"
    }
  ],
  "fragile_areas": [
    {
      "function": "function name",
      "reason": "why maintainers have flagged this as fragile",
      "source": "git | changelog | comment"
    }
  ]
}
```

Only include violations that are explicit in the evidence — do not infer or fabricate.
If a signal is ambiguous, omit it.

**OUTPUT THE JSON NOW.**
