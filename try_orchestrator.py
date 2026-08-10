"""
try_orchestrator.py

Runs the FULL pipeline end-to-end: reproduce -> LLM patch -> apply ->
verify -> (retry if needed). Uses Gemini as the LLM backend.

Setup:
    pip install google-generativeai
    export GEMINI_API_KEY=your_key_here   # get one free at https://aistudio.google.com/apikey

Run from the bugfixer/ project root:
    python try_orchestrator.py
"""
import os
from pathlib import Path

from bugfixer.llm_client import GeminiClient
from bugfixer.orchestrator import run_pipeline

REPO = Path("examples/buggy_calc")
DOCKERFILE_DIR = Path("docker")


def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit(
            "Set GEMINI_API_KEY first, e.g.:\n"
            "  export GEMINI_API_KEY=your_key_here"
        )

    llm_client = GeminiClient(api_key=api_key, model="gemini-2.5-flash")

    print("Running full bugfixer pipeline on examples/buggy_calc ...\n")
    result = run_pipeline(
        repo_path=REPO,
        dockerfile_dir=DOCKERFILE_DIR,
        llm_client=llm_client,
        install_command="pip install -e .",
    )

    print(f"\n{'='*60}")
    print(f"SUCCESS: {result.success}")
    print(f"{'='*60}\n")

    for attempt in result.attempts:
        print(f"--- Attempt {attempt.attempt_number}: {attempt.outcome} ---")
        if attempt.explanation:
            print(f"Explanation: {attempt.explanation}")
        if attempt.diff:
            print(f"Diff:\n{attempt.diff}")
        print(f"Detail: {attempt.detail}\n")

    if result.success:
        print("Final verified patch:")
        print(result.final_diff)


if __name__ == "__main__":
    main()
