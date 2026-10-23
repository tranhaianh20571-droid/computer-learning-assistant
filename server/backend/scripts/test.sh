#!/usr/bin/env bash

set -e

# 默认只执行仓库根 tests/；容器内需挂载该目录并显式指定 LEARNING_TEST_ROOT。
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
test_root="${LEARNING_TEST_ROOT:-$repo_root/tests}"
if [[ ! -f "$test_root/test_mirror_sync.py" ]]; then
    echo "Root test suite is missing; mount repository tests/ and set LEARNING_TEST_ROOT." >&2
    exit 1
fi
# Require the caller to select a scope; never run the whole suite implicitly.
if [[ "$#" -eq 0 ]]; then
    echo "Specify test paths or selectors; see tests/README.md." >&2
    exit 2
fi
cd "$test_root/.."
FASTAPI_ENV=development coverage run -m pytest "$@"
coverage report
coverage html --title "Scoped regression"
