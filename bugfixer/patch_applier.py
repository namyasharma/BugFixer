"""
patch_applier.py

Applies an LLM-generated unified diff inside the sandbox, with
validation BEFORE anything is actually written.

Design choices, and why:
- We check `git apply --check` first — this is a dry run that tells us
  whether the diff is even mechanically valid (right file, right line
  numbers, right context) without touching a single file. Cheap and
  catches most hallucinated/malformed diffs immediately.
- We write the diff to a file inside the sandbox and use `git apply`
  rather than `patch`, because git apply is stricter about context
  matching, which means fewer "it sort of applied but to the wrong
  place" surprises.
- Every apply happens on a fresh checkout state — the orchestrator is
  responsible for resetting the repo (`git checkout .`) between retry
  attempts so failed patches don't compound.
"""

from __future__ import annotations

from dataclasses import dataclass

from bugfixer.sandbox import Sandbox

PATCH_FILE_PATH = "/tmp/proposed.patch"


@dataclass
class ApplyResult:
    success: bool
    message: str


def check_patch_applies(sandbox: Sandbox, diff: str) -> ApplyResult:
    """Dry-run check — does NOT modify any files."""
    _write_patch_file(sandbox, diff)
    result = sandbox.run(f"git apply --check {PATCH_FILE_PATH}", network=False)
    if result.exit_code == 0:
        return ApplyResult(success=True, message="Patch applies cleanly.")
    return ApplyResult(success=False, message=result.stderr or result.stdout)


def apply_patch(sandbox: Sandbox, diff: str) -> ApplyResult:
    """
    Actually applies the patch. Call check_patch_applies() first —
    this function assumes that check already passed.
    """
    _write_patch_file(sandbox, diff)
    result = sandbox.run(f"git apply {PATCH_FILE_PATH}", network=False)
    if result.exit_code == 0:
        return ApplyResult(success=True, message="Patch applied.")
    return ApplyResult(success=False, message=result.stderr or result.stdout)


def reset_repo(sandbox: Sandbox) -> ApplyResult:
    """
    Discards any applied patch and returns the repo to its original
    state. MUST be called between retry attempts, or attempt #2's
    patch would be layered on top of attempt #1's (already-failed)
    changes instead of starting fresh.
    """
    result = sandbox.run("git checkout -- . && git clean -fd", network=False)
    if result.exit_code == 0:
        return ApplyResult(success=True, message="Repo reset to clean state.")
    return ApplyResult(success=False, message=result.stderr or result.stdout)


def _write_patch_file(sandbox: Sandbox, diff: str):
    """
    Writes the diff to a file inside the container rather than passing
    it inline on the command line — diffs can contain characters that
    are painful to shell-escape correctly, and this sidesteps that
    entirely.
    """
    # Use a heredoc with a unique delimiter unlikely to collide with
    # diff content, and ensure the diff ends with a trailing newline
    # (git apply is picky about this).
    delimiter = "BUGFIXER_PATCH_EOF"
    content = diff if diff.endswith("\n") else diff + "\n"
    command = f"cat > {PATCH_FILE_PATH} << '{delimiter}'\n{content}{delimiter}"
    sandbox.run(command, network=False)
