"""
try_github.py

Runs the FULL GitHub-integrated pipeline: reads an issue, clones the
real repo, reproduces + fixes + verifies, then opens a PR (or comments
on the issue if it couldn't find a fix).

SETUP (one-time, on GitHub):
    1. Create a small test repo on your GitHub account (public or
       private, your choice). Push a buggy Python package to it with
       a pytest test suite — you can literally push the
       examples/buggy_calc/ folder from this project as a starting point.
    2. Open an issue on that repo. In the issue body, include a line
       exactly like:
           Failing test: tests/test_calc.py::test_add_negative_numbers
    3. Generate a GitHub Personal Access Token (classic or fine-grained)
       with repo read/write + pull-request permissions:
       https://github.com/settings/tokens
    4. export GITHUB_TOKEN=your_token_here
    5. export GEMINI_API_KEY=your_key_here

Run from the bugfixer/ project root:
    python try_github.py --owner your-username --repo your-test-repo --issue 1
"""
import argparse
import os
from pathlib import Path

from bugfixer.github_client import GitHubClient
from bugfixer.llm_client import GeminiClient
from bugfixer.orchestrator import run_pipeline_from_github_issue

DOCKERFILE_DIR = Path("docker")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True, help="GitHub repo owner/org")
    parser.add_argument("--repo", required=True, help="GitHub repo name")
    parser.add_argument("--issue", required=True, type=int, help="Issue number")
    parser.add_argument("--base-branch", default="main")
    args = parser.parse_args()

    github_token = os.environ.get("GITHUB_TOKEN")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    if not github_token:
        raise SystemExit("Set GITHUB_TOKEN first (needs repo + PR permissions).")
    if not gemini_key:
        raise SystemExit("Set GEMINI_API_KEY first.")

    github_client = GitHubClient(token=github_token, repo_owner=args.owner, repo_name=args.repo)
    llm_client = GeminiClient(api_key=gemini_key, model="gemini-3.5-flash")

    print(f"Running bugfixer on {args.owner}/{args.repo} issue #{args.issue} ...\n")
    result = run_pipeline_from_github_issue(
        github_client=github_client,
        issue_number=args.issue,
        dockerfile_dir=DOCKERFILE_DIR,
        llm_client=llm_client,
        base_branch=args.base_branch,
    )

    print(f"\n{'='*60}")
    print(f"SUCCESS: {result.success}")
    print(f"{'='*60}\n")
    for attempt in result.attempts:
        print(f"--- Attempt {attempt.attempt_number}: {attempt.outcome} ---")
        print(f"Detail: {attempt.detail}\n")

    if result.success:
        print("Check the repo on GitHub — a PR should now be open.")
    else:
        print("Check the issue on GitHub — a comment explaining what was tried should be posted.")


if __name__ == "__main__":
    main()
