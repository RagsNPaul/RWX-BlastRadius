#!/usr/bin/env python3
"""Failing test numbers for one patch, written next to it.

  python3 tools/eval/gt.py --sqlglot sqlglot --python .venv/bin/python \
      --tests sample/tests.json --bank bank

Patches named on the command line are run instead of every patch in --bank.
A patch is skipped when <id>.json already sits beside it. Pytest runs in a
copy of --sqlglot with the real process pools (conftest.py is not copied).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlglot", required=True, type=Path, help="pristine sqlglot checkout")
    parser.add_argument("--python", required=True, help="interpreter with pytest, xdist, and pytest-timeout")
    parser.add_argument("--tests", required=True, type=Path, help="sample/tests.json")
    parser.add_argument("--bank", required=True, type=Path, help="directory of *.patch files")
    parser.add_argument("--timeout", type=int, default=120, help="per-test pytest-timeout seconds")
    parser.add_argument("-n", dest="workers", default="auto", help="pytest-xdist worker count")
    parser.add_argument("patches", nargs="*", type=Path, help="run only these patches")
    return parser.parse_args()


def load_tests(path: Path):
    tests = json.loads(path.read_text())["tests"]
    number = {test["id"]: test["number"] for test in tests}
    by_file: dict[str, list[int]] = {}
    for test in tests:
        by_file.setdefault(test["file"], []).append(test["number"])
    return number, by_file


def patches_of(bank: Path, given: list[Path]) -> list[Path]:
    if given:
        return given
    return sorted(bank.glob("*.patch"))


def apply_patch(work: Path, patch: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(patch)],
        cwd=work,
        capture_output=True,
        text=True,
    )


def failing_ids(out_path: Path, number: dict[str, int], by_file: dict[str, list[int]]) -> tuple[list[int], list[str]]:
    failed: set[int] = set()
    unmapped: list[str] = []
    for nodeid in out_path.read_text().split("\n"):
        if not nodeid:
            continue
        if nodeid.startswith("COLLECT::"):
            filename = nodeid[len("COLLECT::"):].split("::")[0]
            failed.update(by_file.get(filename, []))
            continue
        parts = nodeid.split("[", 1)[0].split("::")
        key = "::".join(parts[:3])
        if key in number:
            failed.add(number[key])
        else:
            unmapped.append(nodeid[:120])
    return sorted(failed), unmapped


def run_one(patch: Path, sqlglot: Path, python: str, number, by_file, timeout: int, workers: str) -> list[int]:
    """Copy sqlglot, apply the patch, return the failing test numbers."""
    work = Path(tempfile.mkdtemp(prefix="gt-"))
    try:
        tree = work / "s"
        shutil.copytree(
            sqlglot,
            tree,
            ignore=shutil.ignore_patterns(".git", "conftest.py", "__pycache__"),
            symlinks=True,
        )
        applied = apply_patch(tree, patch.resolve())
        if applied.returncode:
            raise RuntimeError(f"git apply failed\n{applied.stdout}{applied.stderr}")

        out_path = work / "out.txt"
        env = dict(os.environ)
        env["SKIP_INTEGRATION"] = "0"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONPATH"] = os.pathsep.join((str(tree), str(PLUGIN_DIR)))
        env["GT_OUT"] = str(out_path)
        started = time.time()
        proc = subprocess.run(
            [
                python, "-m", "pytest", "tests",
                "-n", str(workers),
                "-q",
                "-p", "no:cacheprovider",
                "-p", "gtplug",
                f"--timeout={timeout}",
            ],
            cwd=tree,
            env=env,
            capture_output=True,
            text=True,
        )
        elapsed = time.time() - started
        tail = (proc.stdout.strip().splitlines() or [""])[-1][:160]
        print(f"{patch.name} {elapsed:.0f}s exit {proc.returncode} {tail}", file=sys.stderr)
        log = proc.stdout + "\n" + proc.stderr
        if not out_path.exists():
            raise RuntimeError(f"pytest wrote no results\n{_clip(log)}")
        if proc.returncode not in (0, 1):
            raise RuntimeError(f"pytest exit {proc.returncode}\n{_clip(log)}")
        if "crashed while running" in log:
            log_path = patch.with_suffix(".log")
            log_path.write_text(log)
            raise RuntimeError(
                "a pytest worker crashed; the failing set is incomplete\n"
                + _clip(log)
                + f"\nfull log: {log_path}"
            )
        failed, unmapped = failing_ids(out_path, number, by_file)
        for nodeid in unmapped:
            print(f"UNMAPPED {nodeid}", file=sys.stderr)
        if unmapped:
            raise RuntimeError(f"{len(unmapped)} failing node ids are not in tests.json")
        return failed
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _clip(log: str, limit: int = 4000) -> str:
    crash = [line for line in log.splitlines() if "crashed" in line.lower() or line.startswith("E   ")]
    body = "\n".join(crash[:40]) if crash else log[-limit:]
    return body[-limit:]


def _retriable(error: BaseException) -> bool:
    text = str(error)
    return "worker crashed" in text or "pytest wrote no results" in text or "pytest exit -" in text


def write_json(path: Path, failing: list[int]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps({"failing": failing}, separators=(",", ":")) + "\n")
    temporary.replace(path)


def main() -> int:
    args = parse_args()
    if not args.sqlglot.is_dir():
        print(f"gt: --sqlglot is not a directory: {args.sqlglot}", file=sys.stderr)
        return 1
    if not args.tests.is_file():
        print(f"gt: --tests not found: {args.tests}", file=sys.stderr)
        return 1
    number, by_file = load_tests(args.tests)
    patches = patches_of(args.bank, args.patches)
    if not patches:
        print(f"gt: no patches in {args.bank}", file=sys.stderr)
        return 1

    failed_run = 0
    for patch in patches:
        if patch.suffix != ".patch" or not patch.is_file():
            print(f"gt: not a patch: {patch}", file=sys.stderr)
            failed_run += 1
            continue
        dest = patch.with_suffix(".json")
        if dest.exists():
            print(f"gt: skip {patch.stem} ({dest.name} exists)", file=sys.stderr)
            continue
        failing = None
        error = None
        for attempt in (1, 2):
            try:
                failing = run_one(
                    patch, args.sqlglot, args.python, number, by_file, args.timeout, args.workers
                )
                break
            except RuntimeError as caught:
                error = caught
                if attempt == 1 and _retriable(caught):
                    print(f"gt: {patch.stem}: retrying after a worker crash", file=sys.stderr)
                    continue
                break
        if failing is None:
            failed_run += 1
            print(f"gt: {patch.stem}: {error}", file=sys.stderr)
            continue
        write_json(dest, failing)
        print(f"gt: {patch.stem} failing {len(failing)} -> {dest}", file=sys.stderr)
    return 1 if failed_run else 0


if __name__ == "__main__":
    sys.exit(main())
