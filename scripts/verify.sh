#!/bin/sh
# Software development checks. Never auto-fixes, commits, pushes or deploys.
# Prerequisite: uv sync --locked --extra private --no-python-downloads
# Node 22+ is required for the JavaScript family.

set -u

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
cd "$root" || exit 1

usage() {
    echo "usage: $0 [all|lint-format|typecheck|tests-python|tests-javascript|package]" >&2
    exit 2
}

lint_format() {
    if [ -n "${VERIFY_LINT_FORMAT_CMD:-}" ]; then
        sh -c "$VERIFY_LINT_FORMAT_CMD"
        return $?
    fi
    rc=0
    uv run --no-sync ruff check . || rc=1
    uv run --no-sync ruff format --check . || rc=1
    return "$rc"
}

typecheck() {
    if [ -n "${VERIFY_TYPECHECK_CMD:-}" ]; then
        sh -c "$VERIFY_TYPECHECK_CMD"
        return $?
    fi
    uv run --no-sync mypy
}

tests_python() {
    if [ -n "${VERIFY_TESTS_PYTHON_CMD:-}" ]; then
        sh -c "$VERIFY_TESTS_PYTHON_CMD"
        return $?
    fi
    uv run --no-sync python -m unittest discover -s tests -v
}

tests_javascript() {
    if [ -n "${VERIFY_TESTS_JAVASCRIPT_CMD:-}" ]; then
        sh -c "$VERIFY_TESTS_JAVASCRIPT_CMD"
        return $?
    fi
    # ponytail: glob keeps newly added tests/ *.mjs in the same family
    node --test tests/*.mjs
}

package() {
    if [ -n "${VERIFY_PACKAGE_CMD:-}" ]; then
        sh -c "$VERIFY_PACKAGE_CMD"
        return $?
    fi
    if [ -n "${VERIFY_OUT_DIR:-}" ]; then
        uv run --no-sync python scripts/check_wheel.py --out-dir "$VERIFY_OUT_DIR" --keep
        return $?
    fi
    uv run --no-sync python scripts/check_wheel.py
}

run_family() {
    name=$1
    echo "=== $name ==="
    case $name in
        lint-format) lint_format ;;
        typecheck) typecheck ;;
        tests-python) tests_python ;;
        tests-javascript) tests_javascript ;;
        package) package ;;
        *) usage ;;
    esac
}

family=${1:-all}

if [ "$family" != "all" ]; then
    run_family "$family"
    exit $?
fi

overall=0
summary=""
for name in lint-format typecheck tests-python tests-javascript package; do
    echo
    if run_family "$name"; then
        echo "RESULT $name PASS"
        summary="$summary
$name	PASS"
    else
        echo "RESULT $name FAIL"
        summary="$summary
$name	FAIL"
        overall=1
    fi
done

echo
echo "=== summary ==="
echo "$summary" | sed '/^$/d'
exit "$overall"
