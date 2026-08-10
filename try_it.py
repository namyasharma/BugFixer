"""
Quick manual test script for milestone 1: sandbox + test runner only.
No LLM, no GitHub - just proves the Docker sandbox + pytest pipeline works.

Run from the bugfixer/ project root:
    python try_it.py
"""
from pathlib import Path
from bugfixer.sandbox import Sandbox, prepare_repo_copy
from bugfixer.test_runner import install_dependencies, run_test_suite, read_source_files

REPO = Path("examples/buggy_calc")
DOCKERFILE_DIR = Path("docker")

def main():
    repo_copy = prepare_repo_copy(REPO)
    print(f"Working on isolated copy at: {repo_copy}")

    with Sandbox(repo_path=repo_copy, dockerfile_dir=DOCKERFILE_DIR) as sb:
        print("\n--- Installing dependencies ---")
        install_result = install_dependencies(sb, "pip install -e .")
        print(install_result.stdout[-500:])

        print("\n--- Running test suite ---")
        result = run_test_suite(sb)
        print(f"Passed: {result.passed_count}/{result.total}")

        for failure in result.failures:
            print(f"\nFAILED: {failure.test_name}")
            print(f"Implicated files: {failure.implicated_files}")
            print(f"Traceback:\n{failure.traceback}")

            source = read_source_files(sb, failure.implicated_files)
            for path, content in source.items():
                print(f"\n--- {path} ---\n{content}")

if __name__ == "__main__":
    main()
