"""
github_client.py

Handles all interaction with GitHub: reading an issue, cloning the
target repo, and — once a fix is verified — pushing a branch and
opening a PR (or commenting on the issue if no fix was found).

Design choices, and why:
- Uses raw `requests` against the GitHub REST API rather than a heavy
  SDK (e.g. PyGithub) — the surface area we need (get issue, create
  branch ref, open PR, comment) is small enough that a thin wrapper is
  easier to read and debug than learning another library's object model.
- Cloning/pushing uses plain `git` via subprocess on the HOST machine,
  NOT inside the Docker sandbox. The sandbox's job is isolating
  untrusted code execution (running tests, applying patches); talking
  to GitHub with your real credentials is a trusted operation and
  doesn't belong inside that boundary.
- The token is passed in the clone/push URL (https://x-access-token:{token}@...)
  rather than stored in git config, so it never persists to disk in
  the repo's .git/config — only lives in the process's argv/env for
  this run.
- extract_test_reference() looks for a specific, deliberately narrow
  pattern in the issue body: a line like `Failing test: path/to/test.py::test_name`.
  This is a v1 constraint (see project roadmap) — natural-language bug
  report parsing is a later phase, not this one.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import requests

GITHUB_API = "https://api.github.com"
TEST_REFERENCE_PATTERN = re.compile(r"Failing test:\s*(\S+)", re.IGNORECASE)


@dataclass
class Issue:
    number: int
    title: str
    body: str
    repo_owner: str
    repo_name: str


@dataclass
class PROpenResult:
    success: bool
    pr_url: str | None
    message: str


class GitHubClient:
    def __init__(self, token: str, repo_owner: str, repo_name: str):
        self.token = token
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        })

    # ---- Reading issues ----------------------------------------------

    def get_issue(self, issue_number: int) -> Issue:
        url = f"{GITHUB_API}/repos/{self.repo_owner}/{self.repo_name}/issues/{issue_number}"
        resp = self.session.get(url)
        resp.raise_for_status()
        data = resp.json()
        return Issue(
            number=data["number"],
            title=data["title"],
            body=data.get("body") or "",
            repo_owner=self.repo_owner,
            repo_name=self.repo_name,
        )

    def extract_test_reference(self, issue: Issue) -> str | None:
        """
        Looks for a line like:
            Failing test: tests/test_calc.py::test_add_negative_numbers
        in the issue body. Returns None if not found — v1 requires the
        issue to explicitly name the failing test (see module docstring).
        """
        match = TEST_REFERENCE_PATTERN.search(issue.body)
        return match.group(1).strip() if match else None

    # ---- Cloning (host git, not sandboxed) ----------------------------

    def clone_repo(self, dest: Path) -> Path:
        """
        Clones the repo to `dest` using the token for auth. Runs on the
        HOST, not in the Docker sandbox — this is a trusted operation
        (using your real credentials), distinct from running untrusted
        repo/test code.
        """
        clone_url = f"https://x-access-token:{self.token}@github.com/{self.repo_owner}/{self.repo_name}.git"
        subprocess.run(
            ["git", "clone", clone_url, str(dest)],
            check=True,
            capture_output=True,
            text=True,
        )
        return dest

    # ---- Branch, commit, push -----------------------------------------

    def create_branch_and_push(
        self,
        repo_path: Path,
        branch_name: str,
        commit_message: str,
        base_branch: str = "main",
    ):
        """
        Assumes `repo_path` already has the fix applied on disk (i.e.
        this is called AFTER the orchestrator verified the patch, on
        the same checkout the fix was proven against — not a fresh
        unpatched clone).
        """
        def run(cmd):
            subprocess.run(cmd, cwd=repo_path, check=True, capture_output=True, text=True)

        run(["git", "checkout", "-b", branch_name])
        run(["git", "add", "-A"])
        run(["git", "commit", "-m", commit_message])
        push_url = f"https://x-access-token:{self.token}@github.com/{self.repo_owner}/{self.repo_name}.git"
        run(["git", "push", push_url, branch_name])

    # ---- PR / comment ---------------------------------------------------

    def open_pull_request(
        self,
        branch_name: str,
        title: str,
        body: str,
        base_branch: str = "main",
    ) -> PROpenResult:
        url = f"{GITHUB_API}/repos/{self.repo_owner}/{self.repo_name}/pulls"
        resp = self.session.post(url, json={
            "title": title,
            "head": branch_name,
            "base": base_branch,
            "body": body,
        })
        if resp.status_code == 201:
            return PROpenResult(success=True, pr_url=resp.json()["html_url"], message="PR opened.")
        return PROpenResult(success=False, pr_url=None, message=resp.text)

    def comment_on_issue(self, issue_number: int, comment: str):
        """Used when the pipeline could NOT fix the bug — never fail silently."""
        url = f"{GITHUB_API}/repos/{self.repo_owner}/{self.repo_name}/issues/{issue_number}/comments"
        resp = self.session.post(url, json={"body": comment})
        resp.raise_for_status()


def build_pr_body(issue_number: int, explanation: str, diff: str, attempt_count: int) -> str:
    """Constructs a clear, reviewable PR description."""
    return (
        f"Fixes #{issue_number}\n\n"
        f"**Automated fix** — generated and verified by bugfixer.\n\n"
        f"**What was wrong:** {explanation}\n\n"
        f"**Verification:** the full test suite was re-run inside an isolated "
        f"sandbox after applying this patch, and all tests passed. "
        f"({attempt_count} attempt{'s' if attempt_count != 1 else ''} made.)\n\n"
        f"```diff\n{diff}\n```\n\n"
        f"Please review before merging — this patch was not written by a human."
    )


def build_failure_comment(attempt_count: int, last_detail: str) -> str:
    """Comment posted when the pipeline could NOT produce a verified fix."""
    return (
        f"🤖 bugfixer attempted to automatically fix this issue but did not "
        f"find a verified fix after {attempt_count} attempt(s).\n\n"
        f"**Last attempt's result:**\n```\n{last_detail}\n```\n\n"
        f"This issue likely needs human investigation."
    )
