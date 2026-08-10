"""
patch_generator.py

Turns a failing-test context (traceback + implicated source files) into
a proposed unified diff, by prompting an LLM.

Design choices, and why:
- We ask for a *unified diff*, not "rewrite the whole file". Diffs are
  smaller (cheaper, less room for the model to drift), easier to
  validate with `git apply --check` before touching anything, and force
  minimal, targeted changes rather than a full rewrite.
- We force structured output (a single fenced diff block, nothing else)
  so parsing doesn't rely on guessing where the model's commentary ends
  and the code begins.
- On retries, we pass in the *previous* patch attempt and *new* failure
  so the model can course-correct instead of repeating the same mistake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class FailingTestContext:
    """Everything we know about the current failure."""
    test_name: str
    traceback: str
    # path -> file contents, for every file implicated in the traceback
    source_files: dict[str, str]
    # populated on retries
    previous_patch: str | None = None
    previous_failure: str | None = None
    attempt_number: int = 1


@dataclass
class PatchProposal:
    raw_response: str
    diff: str | None
    explanation: str | None
    files_touched: list[str] = field(default_factory=list)


SYSTEM_PROMPT = """You are a precise, conservative software engineer fixing a \
specific failing test in an existing codebase.

Rules you must follow:
1. Make the SMALLEST change that fixes the failing test. Do not refactor, \
rename, reformat, or "improve" unrelated code.
2. Only modify files that were provided to you below. Never invent new \
files or touch files you weren't shown.
3. Your fix must not special-case the test itself (e.g. don't hardcode \
the exact expected test value into the source) — fix the actual bug.
4. Respond in exactly this format, nothing else:

EXPLANATION:
<1-3 sentences on what the bug was and how you're fixing it>

DIFF:
```diff
<a valid unified diff, suitable for `git apply`>
```

If you cannot determine a fix from the given context, respond with:
EXPLANATION:
<what additional information you'd need>

DIFF:
```diff
```
"""


def build_prompt(ctx: FailingTestContext) -> str:
    """Constructs the user-turn prompt from a FailingTestContext."""
    parts = [
        f"Failing test: {ctx.test_name}",
        "",
        "Traceback:",
        "```",
        ctx.traceback.strip(),
        "```",
        "",
        "Relevant source files:",
    ]

    for path, content in ctx.source_files.items():
        parts.append(f"\n--- {path} ---")
        parts.append("```python")
        parts.append(content)
        parts.append("```")

    if ctx.previous_patch and ctx.previous_failure:
        parts.append("\n\nNOTE: A previous attempt was made and it did NOT fix the issue.")
        parts.append(f"Attempt #{ctx.attempt_number - 1} produced this diff:")
        parts.append("```diff")
        parts.append(ctx.previous_patch.strip())
        parts.append("```")
        parts.append("\nAfter applying it, the test still failed with:")
        parts.append("```")
        parts.append(ctx.previous_failure.strip())
        parts.append("```")
        parts.append("\nPropose a different fix that addresses this new failure.")

    return "\n".join(parts)


def parse_response(raw_response: str) -> PatchProposal:
    """Extracts explanation + diff from the model's structured response."""
    explanation_match = re.search(
        r"EXPLANATION:\s*(.*?)\s*DIFF:", raw_response, re.DOTALL
    )
    diff_match = re.search(
        r"```diff\s*(.*?)```", raw_response, re.DOTALL
    )

    explanation = explanation_match.group(1).strip() if explanation_match else None
    diff = diff_match.group(1).strip() if diff_match else None

    files_touched = []
    if diff:
        # unified diff file headers look like: +++ b/path/to/file.py
        files_touched = re.findall(r"^\+\+\+ b/(.+)$", diff, re.MULTILINE)

    return PatchProposal(
        raw_response=raw_response,
        diff=diff if diff else None,
        explanation=explanation,
        files_touched=files_touched,
    )


def generate_patch(ctx: FailingTestContext, llm_client) -> PatchProposal:
    """
    llm_client: any object with a `.complete(system: str, user: str) -> str` method.
    Kept generic here so you can swap in Anthropic/OpenAI/local models
    without changing this module.
    """
    prompt = build_prompt(ctx)
    raw_response = llm_client.complete(system=SYSTEM_PROMPT, user=prompt)
    return parse_response(raw_response)


def validate_scope(proposal: PatchProposal, allowed_files: list[str]) -> tuple[bool, str]:
    """
    Cheap safety net: reject patches that touch files outside what we
    showed the model. Call this BEFORE patch_applier.apply().
    """
    if not proposal.diff:
        return False, "No diff produced."

    unexpected = [f for f in proposal.files_touched if f not in allowed_files]
    if unexpected:
        return False, f"Patch touches unexpected files: {unexpected}"

    return True, ""
