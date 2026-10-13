#!/bin/sh
# First run installs this checkout in a local virtual environment.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python="$root/.venv/bin/python"
if [ ! -x "$python" ]; then
    python3 -m venv "$root/.venv"
fi
if ! "$python" -c 'import asciish, pyte' >/dev/null 2>&1; then
    "$python" -m pip install --disable-pip-version-check -e "$root"
fi
exec "$python" -m asciish "$@"
