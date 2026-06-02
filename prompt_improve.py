"""
prompt_improve — the improve decorator for pact-style self-improving prompts.

pact's rule (intent.py, find.py): a prompt is never an inline string — it's a FILE
(prompts/<name>.md) with {{placeholders}}, and after each run it SELF-IMPROVES: the run
is scored, and if the prompt underperformed, an LLM rewrites the file. Over use, prompts
converge toward optimal.

This module is that decorator, with one upgrade over pact's default: the score is
GROUNDED, not LLM-self-assessed. The caller scores a prompt on what its output did
against the SOUND oracle — invariants that survived the skeptic, tests that built, Halmos
verdicts — so the prompt converges toward outputs the execution/proof layer accepts, not
toward what an LLM thinks looks good.

Usage:
    tmpl = load_prompt("sol_invariant_propose")
    out  = ask(render(tmpl, src=contract))            # run the file-backed prompt
    ...                                                # compute a grounded score in [0,1]
    improve_if_weak("sol_invariant_propose", score, transcript, ask)  # rewrite if weak
"""

from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT_DIR = os.path.join(HERE, "prompts")
THRESHOLD = 0.7  # below this grounded score, the prompt is rewritten


def load_prompt(name: str) -> str:
    with open(os.path.join(PROMPT_DIR, f"{name}.md"), encoding="utf-8") as f:
        return f.read()


def save_prompt(name: str, text: str) -> None:
    with open(os.path.join(PROMPT_DIR, f"{name}.md"), "w", encoding="utf-8") as f:
        f.write(text)


def render(template: str, **vars) -> str:
    for k, v in vars.items():
        template = template.replace("{{" + k + "}}", str(v))
    return template


def improve_if_weak(name: str, score: float, transcript: str, ask) -> bool:
    """If the GROUNDED score is below threshold, ask an LLM to rewrite the prompt file
    given a transcript of what went wrong against the oracle. Returns True if rewritten.
    Degrades gracefully (e.g. no API credits) — never raises."""
    if score >= THRESHOLD:
        return False
    try:
        current = load_prompt(name)
        meta = (
            f"This prompt UNDERPERFORMED against a sound oracle (grounded score {score:.2f} "
            f"< {THRESHOLD}). Rewrite it to fix the demonstrated weakness. Keep its required "
            "output format and {{placeholders}} EXACTLY. Return ONLY the improved prompt text.\n\n"
            f"=== WHAT WENT WRONG (oracle transcript) ===\n{transcript}\n\n"
            f"=== CURRENT PROMPT ({name}.md) ===\n{current}"
        )
        improved = ask(meta).strip()
        if improved and len(improved) > 80 and "{{" in improved:
            save_prompt(name, improved)
            print(f"  [improve] rewrote {name}.md (grounded score {score:.2f})")
            return True
    except Exception as exc:  # noqa: BLE001 — improvement is best-effort
        print(f"  [improve] skipped {name} ({type(exc).__name__}: {str(exc)[:60]})")
    return False
