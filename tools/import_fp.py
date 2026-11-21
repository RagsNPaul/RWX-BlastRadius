"""Write maps/import_fp.json: sha256 of each import-time value on the pinned checkout.

The fingerprint runs under `python -S` with the package on PYTHONPATH, the same
command the selector uses on a patched copy.
"""
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "selector", "fingerprint.py")
SOURCE = os.path.join(REPO, "sqlglot")


def once():
    proc = subprocess.run(
        [sys.executable, "-S", SCRIPT],
        cwd="/tmp",
        env={"PYTHONPATH": SOURCE, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"},
        capture_output=True,
        text=True,
    )
    if proc.returncode:
        sys.stderr.write(proc.stderr)
        raise SystemExit(proc.returncode)
    start = proc.stdout.find("{")
    if start < 0:
        raise SystemExit("fingerprint printed no JSON")
    return json.loads(proc.stdout[start:])


def main():
    first, second = once(), once()
    if first != second:
        raise SystemExit("the fingerprint is not stable across two imports")
    path = os.path.join(REPO, "maps", "import_fp.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(first, handle, separators=(",", ":"))
        handle.write("\n")
    os.chmod(path, 0o644)
    print(f"{len(first)} keys, {os.path.getsize(path)} bytes", file=sys.stderr)


if __name__ == "__main__":
    main()
