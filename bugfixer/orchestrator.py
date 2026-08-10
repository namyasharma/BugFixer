"""
orchestrator.py

The main loop: reproduce -> generate patch -> apply -> verify -> retry
if needed. This is the piece that turns the individual components into
an actual working pipeline.

Design choices, and why:
- The repo must be a git repo (patch_applier relies on `git apply` /
  `git checkout`). The example repos should be initialized with git
  before running this.
- Retry budget is capped (default 3) — if the LLM can't fix it in a
  few tries, we stop and report failure rather than looping forever
  or silently giving up after one bad attempt.
- Every attempt is logged into the returned OrchestrationResult, so
  even a failed run gives you useful debugging/demo material (this is
  genuinely good portfolio content: "here's what it tried and why it
  didn't work").
- PR opening is intentionally NOT in this file yet — this orchestrator
  stops once tests pass locally in the sandbox. GitHub integration is
  a separate, later step (github_client.py), kept out of this loop so
  you can validate the fix-and-verify mechanics fully offline first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from bugfixer.llm_client import LLMClient
from bugfixer.patch_applier import apply_patch, check_patch_applies, reset_repo
from bugfixer.patch_generator import FailingTestContext, generate_patch, validate_scope
from bugfixer.sandbox import Sandbox, prepare_repo_copy
from bugfixer.test_runner import (
    expand_with_local_imports,
    install_dependencies,
    read_source_files,
    run_test_suite,
)

DEFAULT_MAX_ATTEMPTS = 3


@dataclass
class AttemptLog:
    attempt_number: int
    diff: str | None
    explanation: str | None
    outcome: str  # "fixed" | "did_not_apply" | "out_of_scope" | "tests_still_failing"
    detail: str


@dataclass
class OrchestrationResult:
    success: bool
    final_diff: str | None
    attempts: list[AttemptLog] = field(default_factory=list)


def run_pipeline(
    repo_path: Path,
    dockerfile_dir: Path,
    llm_client: LLMClient,
    install_command: str = "pip install -e .",
    test_path: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> OrchestrationResult:
    repo_copy = prepare_repo_copy(repo_path)
    attempts: list[AttemptLog] = []

    with Sandbox(repo_path=repo_copy, dockerfile_dir=dockerfile_dir) as sandbox:
        install_dependencies(sandbox, install_command)

        # Baseline run: confirm there IS a failure to fix, and capture it.
        result = run_test_suite(sandbox, test_path)
        if result.passed:
            return OrchestrationResult(
                success=False,
                final_diff=None,
                attempts=[
                    AttemptLog(
                        attempt_number=0,
                        diff=None,
                        explanation=None,
                        outcome="no_failure_found",
                        detail="Test suite passed with no changes — nothing to fix.",
                    )
                ],
            )

        failure = result.failures[0]  # v1: fix one failure at a time
        implicated = expand_with_local_imports(sandbox, failure.implicated_files)
        source_files = read_source_files(sandbox, implicated)

        ctx = FailingTestContext(
            test_name=failure.test_name,
            traceback=failure.traceback,
            source_files=source_files,
        )

        for attempt_num in range(1, max_attempts + 1):
            ctx.attempt_number = attempt_num
            proposal = generate_patch(ctx, llm_client)

            if not proposal.diff:
                attempts.append(AttemptLog(
                    attempt_number=attempt_num,
                    diff=None,
                    explanation=proposal.explanation,
                    outcome="did_not_apply",
                    detail="Model produced no diff.",
                ))
                continue

            in_scope, scope_msg = validate_scope(proposal, list(source_files.keys()))
            if not in_scope:
                attempts.append(AttemptLog(
                    attempt_number=attempt_num,
                    diff=proposal.diff,
                    explanation=proposal.explanation,
                    outcome="out_of_scope",
                    detail=scope_msg,
                ))
                # Feed this back so the next attempt knows to stay in scope
                ctx.previous_patch = proposal.diff
                ctx.previous_failure = f"Rejected before running: {scope_msg}"
                continue

            check = check_patch_applies(sandbox, proposal.diff)
            if not check.success:
                attempts.append(AttemptLog(
                    attempt_number=attempt_num,
                    diff=proposal.diff,
                    explanation=proposal.explanation,
                    outcome="did_not_apply",
                    detail=check.message,
                ))
                ctx.previous_patch = proposal.diff
                ctx.previous_failure = f"Patch failed to apply: {check.message}"
                continue

            apply_result = apply_patch(sandbox, proposal.diff)
            if not apply_result.success:
                attempts.append(AttemptLog(
                    attempt_number=attempt_num,
                    diff=proposal.diff,
                    explanation=proposal.explanation,
                    outcome="did_not_apply",
                    detail=apply_result.message,
                ))
                reset_repo(sandbox)
                continue

            # Re-run tests to VERIFY — this is the step that makes the
            # whole pipeline trustworthy, not just plausible.
            retest = run_test_suite(sandbox, test_path)
            if retest.passed:
                attempts.append(AttemptLog(
                    attempt_number=attempt_num,
                    diff=proposal.diff,
                    explanation=proposal.explanation,
                    outcome="fixed",
                    detail=f"All {retest.total} tests passed after patch.",
                ))
                return OrchestrationResult(
                    success=True,
                    final_diff=proposal.diff,
                    attempts=attempts,
                )

            # Still failing — log it, reset, and feed the new failure
            # into the next attempt's prompt.
            new_failure_detail = (
                retest.failures[0].traceback if retest.failures else retest.raw_stdout
            )
            attempts.append(AttemptLog(
                attempt_number=attempt_num,
                diff=proposal.diff,
                explanation=proposal.explanation,
                outcome="tests_still_failing",
                detail=new_failure_detail,
            ))
            ctx.previous_patch = proposal.diff
            ctx.previous_failure = new_failure_detail
            reset_repo(sandbox)

        return OrchestrationResult(success=False, final_diff=None, attempts=attempts)


def run_pipeline_from_github_issue(
    github_client,
    issue_number: int,
    dockerfile_dir: Path,
    llm_client: LLMClient,
    install_command: str = "pip install -e .",
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_branch: str = "main",
) -> OrchestrationResult:
    """
    Full GitHub-integrated flow: reads the issue, clones the real repo,
    runs the same verified fix-or-fail pipeline as run_pipeline(), and
    then either opens a PR (on success) or comments on the issue with
    what was tried (on failure) — the pipeline never fails silently.

    v1 constraint: the issue body must contain a line like
        Failing test: path/to/test.py::test_name
    Free-form natural-language bug reports are a later phase (see
    project roadmap) — this keeps the "does the fix actually work"
    verification mechanism unchanged from the proven local-repo flow.
    """
    import tempfile
    import subprocess
    from bugfixer.github_client import build_failure_comment, build_pr_body

    issue = github_client.get_issue(issue_number)
    test_ref = github_client.extract_test_reference(issue)
    if not test_ref:
        github_client.comment_on_issue(
            issue_number,
            "🤖 bugfixer couldn't find a `Failing test: path/to/test.py::test_name` "
            "line in this issue, so it doesn't know what to reproduce. "
            "(v1 requires an explicit failing test reference.)",
        )
        return OrchestrationResult(success=False, final_diff=None, attempts=[])

    clone_dir = Path(tempfile.mkdtemp(prefix="bugfixer-clone-")) / "repo"
    github_client.clone_repo(clone_dir)

    result = run_pipeline(
        repo_path=clone_dir,
        dockerfile_dir=dockerfile_dir,
        llm_client=llm_client,
        install_command=install_command,
        test_path=test_ref,
        max_attempts=max_attempts,
    )

    if result.success:
        # Re-apply the verified diff to the actual clone (run_pipeline
        # worked on an isolated copy inside the sandbox — see
        # prepare_repo_copy — so we apply the same proven diff here on
        # the clone we intend to push from).
        patch_file = clone_dir / "bugfixer_verified.patch"
        patch_file.write_text(result.final_diff)
        subprocess.run(
            ["git", "apply", str(patch_file)],
            cwd=clone_dir, check=True, capture_output=True, text=True,
        )
        patch_file.unlink()

        branch_name = f"bugfixer/issue-{issue_number}"
        last_attempt = result.attempts[-1]
        github_client.create_branch_and_push(
            clone_dir,
            branch_name,
            commit_message=f"Fix #{issue_number}: {last_attempt.explanation or 'automated fix'}",
            base_branch=base_branch,
        )
        pr_result = github_client.open_pull_request(
            branch_name=branch_name,
            title=f"Fix: {issue.title}",
            body=build_pr_body(
                issue_number, last_attempt.explanation or "", result.final_diff, len(result.attempts)
            ),
            base_branch=base_branch,
        )
        if not pr_result.success:
            github_client.comment_on_issue(
                issue_number,
                f"🤖 bugfixer found and verified a fix, but failed to open a PR: {pr_result.message}",
            )
    else:
        last_detail = result.attempts[-1].detail if result.attempts else "No attempts were made."
        github_client.comment_on_issue(
            issue_number,
            build_failure_comment(len(result.attempts), last_detail),
        )

    return result
