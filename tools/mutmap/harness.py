#!/usr/bin/env python3
"""Warm worker: import the pinned sqlglot once, then fork a child per mutant.

A child with new_line is None does a no-op swap (each live function keeps its
own code object) and runs the requested tests. Mutation of the source line is
left to the caller; this module only applies a swap when new_line is None.
"""
from __future__ import annotations

import ast
import math
import concurrent.futures as cf
import gc
import importlib
import io
import json
import os
import pickle
import pkgutil
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


def _in_function(tree: ast.AST, line: int) -> bool:
    """True when `line` sits inside a function or method, including one nested in a statement."""
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if not hasattr(child, "lineno") or not child.lineno <= line <= child.end_lineno:
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return True
            stack.append(child)
    return False


def lines_in_functions(source: str, lines: list[int]) -> bool:
    if not lines:
        return False
    tree = ast.parse(source)
    return all(_in_function(tree, line) for line in lines)


def _norm_consts(code: types.CodeType):
    out = []
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            out.append((const.co_qualname, const.co_firstlineno, const.co_code, _norm_consts(const)))
        else:
            out.append(const)
    return tuple(out)


def _same_code(old: types.CodeType, new: types.CodeType) -> bool:
    return (
        old.co_code == new.co_code
        and old.co_names == new.co_names
        and old.co_varnames == new.co_varnames
        and _norm_consts(old) == _norm_consts(new)
    )


def _index_codes(module_code: types.CodeType) -> dict[tuple[str, int], list[types.CodeType]]:
    found: dict[tuple[str, int], list[types.CodeType]] = {}

    def walk(code: types.CodeType) -> None:
        found.setdefault((code.co_qualname, code.co_firstlineno), []).append(code)
        for const in code.co_consts:
            if isinstance(const, types.CodeType):
                walk(const)

    walk(module_code)
    return found


class Worker:
    def __init__(self, checkout: Path, private: Path, catalog: dict[int, dict]):
        self.checkout = checkout
        self.private = private
        self.catalog = catalog
        self.by_code: dict[types.CodeType, list[types.FunctionType]] = {}
        self.trees: dict[str, dict[tuple[str, int], types.CodeType]] = {}
        self.by_line: dict[tuple[str, int], list[types.FunctionType]] = {}
        self.caches: list = []
        self._seconds: dict[tuple[str, str], float] = {}
        self._prep_key: tuple[int, ...] | None = None
        self._prep_groups: list = []
        self.rss = 0

    @classmethod
    def warm(cls, checkout: Path, tests_path: Path) -> Worker:
        """Import the private sqlglot and every test module. Prints rss_bytes."""
        os.environ["SKIP_INTEGRATION"] = "0"
        sys.dont_write_bytecode = True
        # Tests spawn `python`. The repo root is a namespace package named sqlglot, so the
        # child must run with the checkout as cwd or that shadow wins over the real package.
        os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")
        checkout = checkout.resolve()
        os.chdir(checkout)
        print("harness: copying sqlglot", file=sys.stderr, flush=True)
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
            module_name = _module_name(checkout, checkout / path)
            catalog[test["number"]] = {
                "id": test["id"],
                "file": path,
                "qual": qual,
                "module": module_name,
                "seconds": test["seconds"],
            }

        print("harness: indexing code objects", file=sys.stderr, flush=True)
        worker = cls(checkout, private, catalog)
        worker._seconds = {
            (spec["module"], spec["qual"]): spec["seconds"] for spec in catalog.values()
        }
        worker._index()
        worker._collect_caches()
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

    def _collect_caches(self) -> None:
        found = []
        seen: set[int] = set()
        stack = []
        for name, mod in sys.modules.items():
            if mod is not None and (name == "sqlglot" or name.startswith("sqlglot.")):
                stack.extend(vars(mod).values())
        while stack:
            obj = stack.pop()
            ident = id(obj)
            if ident in seen:
                continue
            seen.add(ident)
            module = getattr(obj, "__module__", "") or ""
            if isinstance(obj, type) and module.startswith("sqlglot"):
                stack.extend(vars(obj).values())
            clear = getattr(obj, "cache_clear", None)
            if callable(clear) and module.startswith("sqlglot"):
                found.append(obj)
        self.caches = found

    def _clear_caches(self) -> None:
        for fn in self.caches:
            try:
                fn.cache_clear()
            except Exception:
                pass

    def _abs(self, file: str) -> str:
        return str(self.private / file)

    def prepare(self, test_numbers: list[int]) -> None:
        """Build unittest suites in the parent so a child does not pay for loading them."""
        key = tuple(test_numbers)
        if self._prep_key == key:
            return
        grouped: dict[str, list[int]] = {}
        for number in test_numbers:
            spec = self.catalog.get(number)
            if spec is None:
                continue
            grouped.setdefault(spec["module"], []).append(number)
        groups = []
        for module_name, numbers in grouped.items():
            module = sys.modules.get(module_name)
            if module is None:
                groups.append((None, numbers, {}))
                continue
            names = [self.catalog[number]["qual"] for number in numbers]
            by_qual = {self.catalog[number]["qual"]: number for number in numbers}
            suite = unittest.defaultTestLoader.loadTestsFromNames(names, module)
            groups.append((suite, numbers, by_qual))
        self._prep_key = key
        self._prep_groups = groups

    def _swap_file(self, file: str, new_source: str) -> int:
        filename = self._abs(file)
        old_source = (self.private / file).read_text(encoding="utf-8")
        old_ix = _index_codes(compile(old_source, filename, "exec"))
        new_ix = _index_codes(compile(new_source, filename, "exec"))
        live: dict[tuple[str, int], list[tuple[types.CodeType, list]]] = {}
        for code, funcs in self.by_code.items():
            if code.co_filename != filename:
                continue
            live.setdefault((code.co_qualname, code.co_firstlineno), []).append((code, funcs))
        swapped = 0
        for key, old_codes in old_ix.items():
            if key[0] == "<module>":
                continue
            new_codes = new_ix.get(key, [])
            lives = live.get(key, [])
            for index, old_code in enumerate(old_codes):
                if index >= len(new_codes):
                    break
                new_code = new_codes[index]
                if _same_code(old_code, new_code):
                    continue
                for live_code, funcs in lives:
                    if live_code.co_code != old_code.co_code:
                        continue
                    for fn in funcs:
                        fn.__code__ = new_code
                    swapped += 1
        return swapped

    def _noop_swap(self, file: str, line: int) -> None:
        for func in self.by_line.get((file, line), ()):
            func.__code__ = func.__code__

    def _run_tests(self, test_numbers: list[int], timeout: int) -> list[int]:
        if self._prep_key == tuple(test_numbers):
            groups = self._prep_groups
        else:
            self.prepare(test_numbers)
            groups = self._prep_groups
        failing: list[int] = []
        for suite, numbers, by_qual in groups:
            if suite is None:
                failing.extend(numbers)
                continue
            stream = io.StringIO()
            result = unittest.TextTestRunner(
                stream=stream, verbosity=0, resultclass=_result_class(timeout, self._seconds)
            ).run(suite)
            if result.wasSuccessful():
                continue
            seen: set[int] = set()
            bad = list(result.failures) + list(result.errors)
            for item in getattr(result, "unexpectedSuccesses", ()):
                bad.append((item, "") if not isinstance(item, tuple) else item)
            for test, _tb in bad:
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

    def _spawn(self, work, test_numbers: list[int], kill_after: float) -> dict:
        fd, path = tempfile.mkstemp(prefix="mutmap-")
        os.close(fd)
        sys.stdout.flush()
        sys.stderr.flush()
        started = time.perf_counter()
        pid = os.fork()
        if pid == 0:
            try:
                os.setpgrp()
            except Exception:
                pass
            try:
                payload = work()
            except BaseException as exc:
                payload = {
                    "failing": list(test_numbers),
                    "test_seconds": 0.0,
                    "status": "error",
                    "error": repr(exc)[:500],
                    "swapped": 0,
                }
            try:
                Path(path).write_text(json.dumps(payload), encoding="utf-8")
            except Exception:
                pass
            os._exit(0)

        deadline = time.perf_counter() + kill_after
        timed_out = False
        status = 0
        rusage = None
        while True:
            wpid, status, rusage = os.wait4(pid, os.WNOHANG)
            if wpid == pid:
                break
            if time.perf_counter() >= deadline:
                timed_out = True
                _kill_group(pid)
                _pid, status, rusage = os.wait4(pid, 0)
                break
            time.sleep(0.05)
        # A test subprocess can outlive the child and keep burning CPU. It is in this group.
        if not timed_out:
            _kill_group(pid)
        wall = time.perf_counter() - started
        try:
            raw = Path(path).read_bytes()
        except OSError:
            raw = b""
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        if not raw:
            report = {
                "failing": list(test_numbers),
                "test_seconds": 0.0,
                "status": "timeout" if timed_out else "error",
                "error": f"child status {status}",
                "swapped": 0,
            }
        else:
            try:
                report = json.loads(raw)
            except json.JSONDecodeError:
                report = {
                    "failing": list(test_numbers),
                    "test_seconds": 0.0,
                    "status": "timeout" if timed_out else "error",
                    "error": "truncated child report",
                    "swapped": 0,
                }
            if timed_out:
                report["status"] = "timeout"
        report["wall"] = wall
        report["cpu"] = (rusage.ru_utime + rusage.ru_stime) if rusage is not None else 0.0
        report["overhead"] = wall - float(report.get("test_seconds") or 0.0)
        report["killed"] = timed_out
        return report

    def run_child(
        self,
        file: str,
        line: int,
        new_line: str | None,
        test_numbers: list[int],
        timeout: int = 20,
        kill_after: float | None = None,
    ) -> dict:
        """Fork. new_line is None for a no-op swap; a string replaces that source line."""
        if kill_after is None:
            kill_after = max(120.0, 20.0 + float(timeout) * max(1, len(test_numbers)) * 0.05)
        return self._spawn(
            lambda: self._child(file, line, new_line, test_numbers, timeout),
            test_numbers,
            kill_after,
        )

    def run_sources(
        self,
        updates: dict[str, str],
        changed: dict[str, list[int]],
        test_numbers: list[int],
        timeout: int = 20,
        kill_after: float = 60.0,
    ) -> dict:
        """Fork, swap every changed function in each file, then run test_numbers."""
        self.prepare(test_numbers)
        return self._spawn(
            lambda: self._child_sources(updates, changed, test_numbers, timeout),
            test_numbers,
            kill_after,
        )

    def _child(self, file: str, line: int, new_line: str | None, test_numbers: list[int], timeout: int) -> dict:
        if new_line is None:
            self._noop_swap(file, line)
            self._clear_caches()
            started = time.perf_counter()
            failing = self._run_tests(test_numbers, timeout)
            return {
                "failing": failing,
                "test_seconds": time.perf_counter() - started,
                "status": "ok",
                "swapped": 0,
            }
        source = (self.private / file).read_text(encoding="utf-8")
        lines = source.split("\n")
        if line < 1 or line > len(lines):
            return {
                "failing": list(test_numbers),
                "test_seconds": 0.0,
                "status": "error",
                "error": f"line {line} is outside {file}",
                "swapped": 0,
            }
        lines[line - 1] = new_line
        return self._child_sources({file: "\n".join(lines)}, {file: [line]}, test_numbers, timeout)

    def _child_sources(
        self,
        updates: dict[str, str],
        changed: dict[str, list[int]],
        test_numbers: list[int],
        timeout: int,
    ) -> dict:
        for file, line_numbers in changed.items():
            source = (self.private / file).read_text(encoding="utf-8")
            if not lines_in_functions(source, line_numbers):
                return {
                    "failing": [],
                    "test_seconds": 0.0,
                    "status": "import_time",
                    "swapped": 0,
                }
        swapped = 0
        for file, new_source in updates.items():
            swapped += self._swap_file(file, new_source)
        self._clear_caches()
        started = time.perf_counter()
        failing = self._run_tests(test_numbers, timeout)
        return {
            "failing": failing,
            "test_seconds": time.perf_counter() - started,
            "status": "ok",
            "swapped": swapped,
        }


def _alarm_seconds(timeout: int, recorded: float, qual: str) -> int:
    """Integer alarm, capped by `timeout`, above this runner's unmutated time.

    test_tpcds is about 5.4s unmutated and has run past a 7s alarm under load,
    so a recording of 2s or more gets 9s. test_qualify_columns (about 1.4s)
    gets 3s. Tests recorded under 0.25s stayed under 1s and get 1s, which is
    what cuts off the serde blow-up. Everything else gets 2s.
    """
    del qual
    if recorded >= 2.0:
        scaled = 9
    elif recorded >= 1.5:
        scaled = math.ceil(recorded * 3)
    elif recorded >= 1.3:
        scaled = 3
    elif recorded < 0.05:
        scaled = 3
    elif recorded < 0.25:
        scaled = 1
    else:
        scaled = 2
    return min(int(timeout), max(1, scaled))


def _result_class(timeout: int, seconds_of: dict[tuple[str, str], float]):
    class TimedResult(unittest.TextTestResult):
        def startTest(self, test):
            super().startTest(test)
            case = test
            while hasattr(case, "test_case"):
                case = case.test_case
            qual = f"{case.__class__.__name__}.{getattr(case, '_testMethodName', '')}"
            recorded = seconds_of.get((case.__class__.__module__, qual), float(timeout))
            budget = _alarm_seconds(timeout, recorded, qual)
            if os.environ.get("HARNESS_TRACE"):
                with open("/tmp/mutmap-current-test", "a", encoding="utf-8") as handle:
                    handle.write(f"{time.perf_counter():.3f} alarm {budget} rec {recorded:.3f} {test}\n")
            signal.signal(signal.SIGALRM, _alarm)
            signal.alarm(budget)

        def stopTest(self, test):
            signal.alarm(0)
            super().stopTest(test)

    return TimedResult


class _TestTimeout(BaseException):
    """Not an Exception, so `except Exception` inside a test cannot swallow the alarm."""


def _alarm(signum, frame):
    raise _TestTimeout("test alarm")


def _kill_group(pid: int) -> None:
    """SIGKILL the child's process group. The child called setpgrp, so this is not our group."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


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
