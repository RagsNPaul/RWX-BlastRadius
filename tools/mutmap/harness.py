#!/usr/bin/env python3
"""Warm worker: import the pinned sqlglot once, then fork a child per mutant.

A child with new_line is None does a no-op swap (each live function keeps its
own code object) and runs the requested tests. Mutation of the source line is
left to the caller; this module only applies a swap when new_line is None.
"""
from __future__ import annotations

import concurrent.futures as cf
import gc
import importlib
import io
import json
import os
import pickle
import pkgutil
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path


class InlineExecutor(cf.Executor):
    """Same inline pool as tools/conftest_map.py, including the initializer call."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *a, **k):
        future = cf.Future()
        try:
            fn, a, k = pickle.loads(pickle.dumps((fn, a, k)))
            future.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, *a, **k):
        pass


def rss_bytes(pid: int | None = None) -> int:
    """Current resident set, in bytes. macOS `ps` reports RSS in kilobytes."""
    raw = subprocess.check_output(
        ["ps", "-o", "rss=", "-p", str(os.getpid() if pid is None else pid)],
        text=True,
    )
    return int(raw.strip()) * 1024


def _walk_code(code: types.CodeType, bucket: dict[tuple[str, int], types.CodeType]) -> None:
    bucket.setdefault((code.co_qualname, code.co_firstlineno), code)
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            _walk_code(const, bucket)


def _module_name(checkout: Path, path: Path) -> str:
    rel = path.relative_to(checkout).with_suffix("")
    return ".".join(rel.parts)


def _split_id(test_id: str) -> tuple[str, str]:
    path, _, rest = test_id.partition("::")
    return path, rest.replace("::", ".")


class Worker:
    def __init__(self, checkout: Path, private: Path, catalog: dict[int, dict]):
        self.checkout = checkout
        self.private = private
        self.catalog = catalog
        self.by_code: dict[types.CodeType, list[types.FunctionType]] = {}
        self.trees: dict[str, dict[tuple[str, int], types.CodeType]] = {}
        self.by_line: dict[tuple[str, int], list[types.FunctionType]] = {}
        self.rss = 0

    @classmethod
    def warm(cls, checkout: Path, tests_path: Path) -> Worker:
        """Import the private sqlglot and every test module. Prints rss_bytes."""
        os.environ["SKIP_INTEGRATION"] = "0"
        sys.dont_write_bytecode = True
        print("harness: copying sqlglot", file=sys.stderr, flush=True)
        checkout = checkout.resolve()
        private = Path(tempfile.mkdtemp(prefix="mutmap-worker-"))
        shutil.copytree(
            checkout / "sqlglot",
            private / "sqlglot",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        sys.path.insert(0, str(private))
        sys.path.insert(1, str(checkout))
        cf.ProcessPoolExecutor = InlineExecutor

        print("harness: importing sqlglot", file=sys.stderr, flush=True)
        import sqlglot

        for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
            if info.name.endswith(".__main__"):
                continue
            try:
                importlib.import_module(info.name)
            except Exception as exc:
                print(f"harness: cannot import {info.name}: {exc!r}", file=sys.stderr)

        print("harness: importing tests", file=sys.stderr, flush=True)
        for path in sorted((checkout / "tests").rglob("*.py")):
            # gen_fixtures.py is a script: importing it rewrites TPC files on another machine.
            if path.name in {"conftest.py", "gen_fixtures.py"}:
                continue
            name = _module_name(checkout, path)
            try:
                importlib.import_module(name)
            except Exception as exc:
                print(f"harness: cannot import {name}: {exc!r}", file=sys.stderr)

        catalog: dict[int, dict] = {}
        for test in json.loads(tests_path.read_text())["tests"]:
            path, qual = _split_id(test["id"])
            catalog[test["number"]] = {
                "id": test["id"],
                "file": path,
                "qual": qual,
                "module": _module_name(checkout, checkout / path),
                "seconds": test["seconds"],
            }

        print("harness: indexing code objects", file=sys.stderr, flush=True)
        worker = cls(checkout, private, catalog)
        worker._index()
        gc.collect()
        gc.freeze()
        worker.rss = rss_bytes()
        print(f"rss_bytes {worker.rss}", flush=True)
        return worker

    def _index(self) -> None:
        prefix = str(self.private) + os.sep
        line_sets: dict[tuple[str, int], set[types.FunctionType]] = {}
        objects = gc.get_objects()
        for obj in objects:
            if isinstance(obj, types.FunctionType):
                code = obj.__code__
                if not code.co_filename.startswith(prefix):
                    continue
                self.by_code.setdefault(code, []).append(obj)
                _walk_code(code, self.trees.setdefault(code.co_filename, {}))
                rel = code.co_filename[len(prefix):]
                for _start, _end, lineno in code.co_lines():
                    if not lineno or lineno <= 0:
                        continue
                    line_sets.setdefault((rel, lineno), set()).add(obj)
            elif isinstance(obj, types.CodeType) and obj.co_filename.startswith(prefix):
                self.trees.setdefault(obj.co_filename, {}).setdefault(
                    (obj.co_qualname, obj.co_firstlineno), obj
                )
        self.by_line = {key: list(funcs) for key, funcs in line_sets.items()}

    def _noop_swap(self, file: str, line: int) -> None:
        for func in self.by_line.get((file, line), ()):
            func.__code__ = func.__code__

    def _run_tests(self, test_numbers: list[int], timeout: int) -> list[int]:
        grouped: dict[str, list[int]] = {}
        failing: list[int] = []
        for number in test_numbers:
            spec = self.catalog.get(number)
            if spec is None:
                failing.append(number)
                continue
            grouped.setdefault(spec["module"], []).append(number)

        for module_name, numbers in grouped.items():
            module = sys.modules.get(module_name)
            if module is None:
                failing.extend(numbers)
                continue
            names = [self.catalog[number]["qual"] for number in numbers]
            by_qual = {self.catalog[number]["qual"]: number for number in numbers}
            try:
                suite = unittest.defaultTestLoader.loadTestsFromNames(names, module)
            except Exception:
                failing.extend(numbers)
                continue
            stream = io.StringIO()
            result = unittest.TextTestRunner(
                stream=stream, verbosity=0, resultclass=_result_class(timeout)
            ).run(suite)
            if result.wasSuccessful():
                continue
            seen: set[int] = set()
            for test, _tb in (*result.failures, *result.errors):
                case = test
                while hasattr(case, "test_case"):
                    case = case.test_case
                qual = getattr(case, "_testMethodName", "")
                cls = case.__class__.__name__
                number = by_qual.get(f"{cls}.{qual}", by_qual.get(qual))
                if number is None:
                    failing.extend(numbers)
                    seen.update(numbers)
                    break
                if number not in seen:
                    seen.add(number)
                    failing.append(number)
        return failing

    def run_child(self, file: str, line: int, new_line: str | None, test_numbers: list[int], timeout: int = 20) -> dict:
        """Fork. new_line is None for a no-op swap. Returns the child's report plus overhead."""
        read_fd, write_fd = os.pipe()
        sys.stdout.flush()
        sys.stderr.flush()
        started = time.perf_counter()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                payload = self._child(file, line, new_line, test_numbers, timeout)
            except BaseException as exc:
                payload = {
                    "failing": list(test_numbers),
                    "test_seconds": 0.0,
                    "status": "error",
                    "error": repr(exc)[:400],
                }
            os.write(write_fd, json.dumps(payload).encode())
            os.close(write_fd)
            os._exit(0)

        os.close(write_fd)
        parent_limit = max(120.0, 20.0 + float(timeout) * max(1, len(test_numbers)) * 0.05)
        deadline = time.perf_counter() + parent_limit
        chunks: list[bytes] = []
        timed_out = False
        while True:
            remain = deadline - time.perf_counter()
            if remain <= 0:
                timed_out = True
                break
            ready, _, _ = select.select([read_fd], [], [], min(remain, 1.0))
            if not ready:
                continue
            buf = os.read(read_fd, 65536)
            if not buf:
                break
            chunks.append(buf)
        os.close(read_fd)
        if timed_out:
            os.kill(pid, signal.SIGKILL)
        _pid, status = os.waitpid(pid, 0)
        wall = time.perf_counter() - started
        raw = b"".join(chunks)
        if not raw:
            report = {
                "failing": list(test_numbers),
                "test_seconds": 0.0,
                "status": "timeout" if timed_out else "error",
                "error": f"child status {status}",
            }
        else:
            report = json.loads(raw)
        report["wall"] = wall
        report["overhead"] = wall - float(report.get("test_seconds") or 0.0)
        return report

    def _child(self, file: str, line: int, new_line: str | None, test_numbers: list[int], timeout: int) -> dict:
        if new_line is not None:
            raise RuntimeError("source mutation is not applied by the warm worker")
        self._noop_swap(file, line)
        started = time.perf_counter()
        failing = self._run_tests(test_numbers, timeout)
        return {
            "failing": failing,
            "test_seconds": time.perf_counter() - started,
            "status": "ok",
        }


def _result_class(timeout: int):
    class TimedResult(unittest.TextTestResult):
        def startTest(self, test):
            super().startTest(test)
            signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(timeout)

        def stopTest(self, test):
            signal.alarm(0)
            super().stopTest(test)

    return TimedResult


def _alarm(signum, frame):
    raise TimeoutError("test alarm")


def available_gib() -> float:
    page = os.sysconf("SC_PAGE_SIZE")
    pages = {}
    for line in subprocess.check_output(["vm_stat"], text=True).splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        digits = "".join(ch for ch in val if ch.isdigit())
        if digits:
            pages[key.strip()] = int(digits)
    count = pages["Pages free"] + pages["Pages inactive"] + pages["Pages speculative"]
    return count * page / (1024 ** 3)


def _covered(maps: Path, tests_path: Path) -> list[tuple[str, int, list[int]]]:
    tests = json.loads(tests_path.read_text())["tests"]
    fast = {test["number"] for test in tests if test["seconds"] <= 3.0}
    sets = json.loads((maps / "sets.json").read_text())
    rows = []
    root = maps / "lines"
    for path in root.rglob("*.json"):
        rel = str(path.relative_to(root))[: -len(".json")]
        table = json.loads(path.read_text())
        for line, index in table.items():
            numbers = [number for number in sets[index] if number in fast]
            if numbers:
                rows.append((rel, int(line), numbers))
    return rows


def check(root: Path) -> int:
    import math
    import random

    checkout = root / "sqlglot"
    tests_path = root / "sample" / "tests.json"
    started = time.perf_counter()
    worker = Worker.warm(checkout, tests_path)
    warmup = time.perf_counter() - started
    rss_gib = worker.rss / (1024 ** 3)
    avail = available_gib()
    workers = min(9, math.floor(avail / (rss_gib * 1.5))) if rss_gib else 0
    print(f"warmup_s {warmup:.3f}", flush=True)
    print(f"available_gib {avail:.3f}", flush=True)
    print(f"rss_gib {rss_gib:.3f}", flush=True)
    print(f"workers {workers}", flush=True)

    rng = random.Random(0)
    rows = _covered(root / "maps", tests_path)
    sample = rng.sample(rows, 20)
    failures = []
    overheads = []
    for index, (file, line, numbers) in enumerate(sample, 1):
        print(f"child {index}/20 {file}:{line} tests {len(numbers)}", file=sys.stderr, flush=True)
        report = worker.run_child(file, line, None, numbers, timeout=20)
        overheads.append(report["overhead"])
        if report["failing"] or report.get("status") != "ok":
            failures.append((file, line, report))
    mean_overhead = sum(overheads) / len(overheads)
    print(f"noop_failures {len(failures)}", flush=True)
    print(f"mean_overhead_s {mean_overhead:.4f}", flush=True)
    for file, line, report in failures:
        shown = report["failing"][:8]
        print(
            f"fail {file}:{line} status {report.get('status')} n {len(report['failing'])} {shown} {report.get('error', '')}",
            flush=True,
        )
    ok = warmup <= 60 and workers >= 4 and not failures and mean_overhead <= 0.10
    print("CHECK_PASS" if ok else "CHECK_FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    here = Path(__file__).resolve().parents[2]
    sys.exit(check(here))
