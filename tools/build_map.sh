#!/usr/bin/env bash
# Build the coverage map once, locally (not in the Docker build).
# Usage: bash tools/build_map.sh        (from repo root; ~4 min on 2 cores)
set -euo pipefail
ROOT=$(pwd)
SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
[ -d sqlglot ] || { git clone -q https://github.com/tobymao/sqlglot.git sqlglot && git -C sqlglot checkout -q $SHA; }
[ -d .venv ] || { python3.13 -m venv .venv && .venv/bin/pip -q install -r requirements.lock "coverage==7.*"; }
# Run optimizer/executor ProcessPool work inline so coverage credits the calling test.
cp tools/conftest_map.py sqlglot/conftest.py
cat > .coveragerc.map <<RC
[run]
source = sqlglot
dynamic_context = test_function
data_file = $ROOT/.coverage
RC
rm -f .coverage*
(cd sqlglot && SKIP_INTEGRATION=0 PYTHONPATH=. ../.venv/bin/coverage run --rcfile=../.coveragerc.map -m pytest tests -q -p no:cacheprovider)
.venv/bin/python tools/make_map.py
