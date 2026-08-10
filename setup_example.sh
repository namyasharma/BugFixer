#!/bin/bash
# One-time setup: turns examples/buggy_calc into a real git repo.
# patch_applier.py relies on `git apply` / `git checkout`, so the
# target repo must have git initialized with a committed baseline —
# that baseline is what "git checkout -- ." resets to between retries.
set -e
cd "$(dirname "$0")/examples/buggy_calc"
if [ -d .git ]; then
    echo "Already a git repo, skipping."
else
    git init -q
    git add -A
    git commit -q -m "Initial buggy state"
    echo "Initialized examples/buggy_calc as a git repo."
fi
