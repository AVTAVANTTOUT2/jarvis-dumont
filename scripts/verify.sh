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
    rc=0
    uv run --no-sync ruff check . || rc=1
    uv run --no-sync ruff format --check . || rc=1
    return "$rc"
}

typecheck() {
    uv run --no-sync mypy
}

tests_python() {
    uv run --no-sync python -m unittest discover -s tests -v
}

tests_javascript() {
    # ponytail: glob keeps newly added tests/ *.mjs in the same family
    node --test tests/*.mjs
}

package() {
    rc=0
    uv build --no-python-downloads || rc=1
    if [ "$rc" -ne 0 ]; then
        return "$rc"
    fi
    uv run --no-sync python - <<'PY'
from pathlib import Path
from zipfile import ZipFile

wheel = next(Path("dist").glob("*.whl"))
with ZipFile(wheel) as archive:
    names = archive.namelist()
    assert "jarvis_office/control.html" in names
    assert all(n.startswith("jarvis_office/") or ".dist-info/" in n for n in names)
    assert not any(n.endswith((".wav", ".safetensors", ".env", ".toml")) for n in names)
print(f"wheel_ok {wheel.name}")
PY
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
