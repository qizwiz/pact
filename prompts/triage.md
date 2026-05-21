# Prompt: Project Triage

You are building a world model of a software project. Your job is to identify
which files are essential to understanding the project's design — not just
which files are largest or most active, but which ones contain the decisions
that define what this project IS.

## Project context

Name: {{project_name}}
File listing:
{{file_listing}}

README / entry points:
{{readme_excerpt}}

## Your task

Read the above carefully. Then answer:

**1. Project essence** (2-3 paragraphs)
What is this project fundamentally trying to do? Not what it does technically,
but what problem it exists to solve and why that problem matters. Be specific —
reference actual module names, key abstractions you can infer from the file
listing and README.

**2. Key files** (ranked list, 10-15 files)
Which files, if understood deeply, would give someone a complete mental model
of the project? For each file explain WHY it's essential — what design
decision or core abstraction lives there. Do not pick files just because
they're large. Pick files because they encode irreplaceable intent.

**3. Analysis order**
In what order should these files be read to build understanding correctly?
Some files define abstractions that others depend on — identify the dependency
order so the understanding accumulates rather than fragments.

Return JSON only:
{
  "project_essence": "multi-paragraph description",
  "key_files": [
    {
      "path": "relative/path.py",
      "rank": 1,
      "why_essential": "specific reason this file encodes irreplaceable intent",
      "reads_after": []
    }
  ]
}
