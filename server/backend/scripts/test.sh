#!/usr/bin/env bash

set -e
set -x

# 默认只执行仓库根 tests/；容器内需挂载该目录并显式指定 LEARNING_TEST_ROOT。
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
test_root="${LEARNING_TEST_ROOT:-$repo_root/tests}"
if [[ ! -f "$test_root/test_mirror_sync.py" ]]; then
    echo "Root test suite is missing; mount repository tests/ and set LEARNING_TEST_ROOT." >&2
    exit 1
fi
FASTAPI_ENV=development coverage run -m pytest "$test_root/"
coverage report
coverage html --title "${@-coverage}"
