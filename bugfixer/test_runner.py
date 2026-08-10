"""
test_runner.py

Runs the target repo's test suite inside a Sandbox and parses the
results into a structured form the rest of the pipeline can use.

Design choices, and why:
- We rely on pytest-json-report (installed in the Dockerfile) instead
  of parsing pytest's human-readable stdout. Terminal output includes
  color codes, truncation, and formatting that changes between pytest
  versions — a JSON report is a stable contract to parse against.
- We only ever ask for ONE failing test's info at a time in v1
  (the first failure). Multi-bug repos are a v2 problem — trying to
  fix everything at once makes the LLM's job much harder and makes
  failures harder to attribute to a specific patch.
- extract_traceback_context() pulls out the actual source file paths
  and line numbers implicated by the failure, which is what gets
  handed to patch_generator.py — the LLM should only see the files
  that are actually relevant to the bug, not the whole repo.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from bugfixer.sandbox import Sandbox, ExecResult

JSON_REPORT_PATH = "/tmp/report.json"


@dataclass
class TestFailure:
    test_name: str
    traceback: str
    # file paths (relative to repo root) implicated in the traceback
    implicated_files: list[str]


@dataclass
class TestRunResult:
    passed: bool
    total: int
    passed_count: int
    failed_count: int
    failures: list[TestFailure]
    raw_stdout: str


def install_dependencies(sandbox: Sandbox, install_command: str = "pip install -e .") -> ExecResult:
    """
    Installs the target repo's dependencies. Runs WITH network access
    (network=True) since pip needs to reach PyPI. This is the one step
    in the whole pipeline that's allowed to touch the internet.
    """
    return sandbox.run(install_command, network=True)


def run_test_suite(sandbox: Sandbox, test_path: str | None = None) -> TestRunResult:
    """
    Runs pytest inside the sandbox with network disabled and parses the
    JSON report it produces.

    test_path: optionally scope to a single test file/node id
               (e.g. "tests/test_calc.py::test_add_negative_numbers").
               Useful once you already know which test is failing —
               much faster than re-running the whole suite every retry.
    """
    target = test_path or ""
    command = f"pytest {target} --json-report --json-report-file={JSON_REPORT_PATH} -q"

    result = sandbox.run(command, network=False)

    report_cat = sandbox.run(f"cat {JSON_REPORT_PATH}", network=False)
    if report_cat.exit_code != 0 or not report_cat.stdout.strip():
        # pytest-json-report failed to write, or suite errored before
        # collection even happened (e.g. import error) — surface raw output
        return TestRunResult(
            passed=False,
            total=0,
            passed_count=0,
            failed_count=0,
            failures=[
                TestFailure(
                    test_name="<collection error>",
                    traceback=result.stdout + "\n" + result.stderr,
                    implicated_files=[],
                )
            ],
            raw_stdout=result.stdout,
        )

    report = json.loads(report_cat.stdout)
    summary = report.get("summary", {})
    total = summary.get("total", 0)
    passed_count = summary.get("passed", 0)
    failed_count = summary.get("failed", 0)

    failures = []
    for test in report.get("tests", []):
        if test.get("outcome") != "failed":
            continue
        longrepr = test.get("call", {}).get("longrepr", "") or test.get("longrepr", "")
        failures.append(
            TestFailure(
                test_name=test.get("nodeid", "<unknown>"),
                traceback=str(longrepr),
                implicated_files=extract_implicated_files(str(longrepr)),
            )
        )

    return TestRunResult(
        passed=(failed_count == 0 and total > 0),
        total=total,
        passed_count=passed_count,
        failed_count=failed_count,
        failures=failures,
        raw_stdout=result.stdout,
    )


def extract_implicated_files(traceback: str) -> list[str]:
    """
    Pulls file paths out of a pytest traceback. Matches lines like:
        repo/module/thing.py:42: in some_function
    Deliberately conservative (regex, not AST) — good enough to narrow
    down which files to show the LLM, doesn't need to be perfect.
    """
    matches = re.findall(r"([\w./]+\.py):\d+", traceback)
    seen = []
    for m in matches:
        if m not in seen:
            seen.append(m)
    return seen


def expand_with_local_imports(sandbox: Sandbox, files: list[str]) -> list[str]:
    """
    Traceback-based file detection misses the actual buggy file when a
    test fails via wrong-return-value rather than an exception — the
    traceback only ever shows frames where an exception propagated, so
    a function that returns a wrong-but-valid value leaves no frame in
    the failing file itself (only the test file's assert line shows up).

    This expands the file list by parsing each already-implicated
    file's imports and pulling in any matching local (same-repo)
    modules, so e.g. `from calc import add` in a test file also pulls
    in calc.py.
    """
    expanded = list(files)
    for path in files:
        result = sandbox.run(f"cat {path}", network=False)
        if result.exit_code != 0:
            continue
        modules = re.findall(r"^\s*from\s+([\w.]+)\s+import", result.stdout, re.MULTILINE)
        modules += re.findall(r"^\s*import\s+([\w.]+)", result.stdout, re.MULTILINE)
        for mod in modules:
            mod_path = mod.replace(".", "/") + ".py"
            check = sandbox.run(f"test -f {mod_path} && echo FOUND", network=False)
            if "FOUND" in check.stdout and mod_path not in expanded:
                expanded.append(mod_path)
    return expanded


def read_source_files(sandbox: Sandbox, file_paths: list[str]) -> dict[str, str]:
    """
    Reads the content of implicated files out of the sandbox, to hand
    to patch_generator.py. Reading from inside the container (not from
    the host copy) guarantees we're looking at exactly what the test
    run actually saw.
    """
    contents = {}
    for path in file_paths:
        result = sandbox.run(f"cat {path}", network=False)
        if result.exit_code == 0:
            contents[path] = result.stdout
    return contents
