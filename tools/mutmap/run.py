#!/usr/bin/env python3
"""Run one mutation per covered line on warm workers. Resumable."""
from __future__ import annotations

import argparse
import ast
import json
import multiprocessing as mp
import os
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "mutmap"))

from harness import Worker, available_gib  # noqa: E402
from ops import mutations  # noqa: E402

FAST = 3.0
PRIORITY_PREFIXES = (
    "sqlglot/dialects/",
    "sqlglot/generators/",
    "sqlglot/parsers/",
    "sqlglot/optimizer/",
    "sqlglot/expressions/",
    "sqlglot/executor/",
    "sqlglot/typing/",
)
PRIORITY_FILES = {"sqlglot/transforms.py", "sqlglot/serde.py"}


def _priority(path: str) -> int:
    if path in PRIORITY_FILES or path.startswith(PRIORITY_PREFIXES):
        return 0
    return 1


def _load_done(path: Path) -> set[tuple[str, int, str]]:
    done: set[tuple[str, int, str]] = set()
    if not path.is_file():
        return done
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        done.add((row["file"], int(row["line"]), row["op"]))
    return done


def _ordered_lines(root: Path) -> list[tuple[str, int, float, list[int]]]:
    tests = json.loads((root / "sample" / "tests.json").read_text())["tests"]
    seconds = {test["number"]: test["seconds"] for test in tests}
    fast = {test["number"] for test in tests if test["seconds"] <= FAST}
    sets = json.loads((root / "maps" / "sets.json").read_text())
    rows = []
    for path in (root / "maps" / "lines").rglob("*.json"):
        rel = str(path.relative_to(root / "maps" / "lines"))[: -len(".json")]
        table = json.loads(path.read_text())
        for line, index in table.items():
            numbers = sets[index]
            covering = [number for number in numbers if number in fast]
            if not covering:
                continue
            cost = sum(seconds.get(number, 0.0) for number in numbers)
            rows.append((rel, int(line), cost, covering))
    rows.sort(key=lambda row: (_priority(row[0]), row[2], row[0], row[1]))
    return rows


def _replace_line(source: str, line_no: int, new_line: str) -> str:
    lines = source.split("\n")
    lines[line_no - 1] = new_line.split("\n", 1)[0]
    return "\n".join(lines)


def _file_dump(cache: dict[str, str], path: str, source: str) -> str | None:
    if path not in cache:
        try:
            cache[path] = ast.dump(ast.parse(source))
        except SyntaxError:
            cache[path] = ""
    return cache[path] or None


def _accepts(cache: dict[str, str], path: str, source: str, line_no: int, new_line: str) -> bool:
    original = _file_dump(cache, path, source)
    if original is None:
        return False
    try:
        mutated = ast.dump(ast.parse(_replace_line(source, line_no, new_line)))
    except SyntaxError:
        return False
    return mutated != original


def _select(ops: list[tuple[str, str]], which: int) -> list[tuple[str, str]]:
    if which == 1:
        return ops[:1]
    return ops[1:3]


def iter_jobs(root: Path, which: int, done: set[tuple[str, int, str]], limit: int | None):
    """Yield mutants in queue order. `produced` counts resumed rows toward `limit`."""
    checkout = root / "sqlglot"
    sources: dict[str, str] = {}
    dumps: dict[str, str] = {}
    produced = 0
    for rel, line_no, _cost, covering in _ordered_lines(root):
        if rel not in sources:
            file_path = checkout / rel
            if not file_path.is_file():
                continue
            sources[rel] = file_path.read_text(encoding="utf-8")
        lines = sources[rel].split("\n")
        if line_no < 1 or line_no > len(lines):
            continue
        valid = [
            (op, new_line)
            for op, new_line in mutations(lines[line_no - 1])
            if _accepts(dumps, rel, sources[rel], line_no, new_line)
        ]
        for op, new_line in _select(valid, which):
            if limit is not None and produced >= limit:
                return
            produced += 1
            key = (rel, line_no, op)
            if key in done:
                continue
            yield (rel, line_no, op, new_line, covering)


def _status_of(report: dict, covering: list[int]) -> tuple[str, list[int]]:
    status = report.get("status")
    if report.get("killed") or status == "timeout":
        return "timeout", list(covering)
    if status == "import_time":
        return "import_time", []
    if status == "ok":
        return "ok", list(report.get("failing") or [])
    return "error", list(report.get("failing") or covering)


def _serve(job_q, result_q, checkout: str, tests_path: str) -> None:
    worker = Worker.warm(Path(checkout), Path(tests_path))
    while True:
        job = job_q.get()
        if job is None:
            return
        rel, line_no, op, new_line, covering = job
        try:
            report = worker.run_child(rel, line_no, new_line, covering, timeout=20, kill_after=60.0)
            status, failing = _status_of(report, covering)
            result_q.put(
                {
                    "file": rel,
                    "line": line_no,
                    "op": op,
                    "status": status,
                    "failing": failing,
                    "cpu": round(float(report.get("cpu") or 0.0), 6),
                    "wall": round(float(report.get("wall") or 0.0), 6),
                }
            )
        except BaseException as exc:
            result_q.put(
                {
                    "file": rel,
                    "line": line_no,
                    "op": op,
                    "status": "error",
                    "failing": list(covering),
                    "cpu": 0.0,
                    "wall": 0.0,
                    "error": repr(exc)[:300],
                }
            )


def _rss_tree(pids: list[int]) -> int:
    if not pids:
        return 0
    text = os.popen("ps -axo pid=,ppid=,rss=").read()
    rss: dict[int, int] = {}
    children: dict[int, list[int]] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        pid, ppid, kb = int(parts[0]), int(parts[1]), int(parts[2])
        rss[pid] = kb * 1024
        children.setdefault(ppid, []).append(pid)
    total = 0
    stack = list(pids)
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += rss.get(pid, 0)
        stack.extend(children.get(pid, ()))
    return total


def _deadline(until: str, started: datetime) -> datetime:
    hour, minute = (int(part) for part in until.split(":"))
    stop = started.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if stop <= started:
        stop += timedelta(days=1)
    return stop


def _past_until(until: str, started: datetime) -> bool:
    return datetime.now() >= _deadline(until, started)


def _write(handle, row: dict) -> None:
    payload = {key: row[key] for key in ("file", "line", "op", "status", "failing", "cpu", "wall")}
    handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
    handle.flush()
    os.fsync(handle.fileno())


def run(args: argparse.Namespace) -> int:
    root = ROOT
    out = Path(args.out)
    if not out.is_absolute():
        out = root / out
    out.parent.mkdir(parents=True, exist_ok=True)
    done = _load_done(out)
    print(f"queue pass={args.pass_n} already={len(done)}", flush=True)
    jobs = iter_jobs(root, args.pass_n, done, args.limit)
    exhausted = False

    def pull():
        nonlocal exhausted
        if exhausted:
            return None
        try:
            return next(jobs)
        except StopIteration:
            exhausted = True
            return None

    ctx = mp.get_context("spawn")
    job_q = ctx.Queue()
    result_q = ctx.Queue()
    checkout = str(root / "sqlglot")
    tests_path = str(root / "sample" / "tests.json")
    workers = []
    for _ in range(args.workers):
        proc = ctx.Process(target=_serve, args=(job_q, result_q, checkout, tests_path), daemon=True)
        proc.start()
        workers.append(proc)

    stop = {"flag": False}

    def _stop(signum, _frame):
        stop["flag"] = True
        print(f"signal {signum}", flush=True)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    started = datetime.now()
    clock = time.perf_counter()
    submitted = 0
    finished = 0
    max_rss = 0
    max_frac = 0.0
    last_sample = 0.0
    rss = 0
    frac = 0.0
    window = args.workers * 2
    handle = out.open("a", encoding="utf-8")
    try:
        while not stop["flag"]:
            if _past_until(args.until, started):
                print("until reached", flush=True)
                break
            while submitted - finished < window and not stop["flag"]:
                job = pull()
                if job is None:
                    break
                job_q.put(job)
                submitted += 1
            if exhausted and finished >= submitted:
                break
            try:
                row = result_q.get(timeout=1.0)
            except Exception:
                row = None
            now = time.perf_counter()
            if now - last_sample >= 1.0:
                rss = _rss_tree([proc.pid for proc in workers if proc.pid])
                avail = available_gib() * (1024 ** 3)
                frac = rss / avail if avail else 1.0
                max_rss = max(max_rss, rss)
                max_frac = max(max_frac, frac)
                last_sample = now
            if row is None:
                alive = [proc for proc in workers if proc.is_alive()]
                if not alive and finished < submitted:
                    print("workers exited early", flush=True)
                    break
                continue
            _write(handle, row)
            finished += 1
            if finished % 100 == 0:
                elapsed = max(time.perf_counter() - clock, 1e-6)
                rate = finished / elapsed * 3600.0
                eta = max(0.0, (_deadline(args.until, started) - datetime.now()).total_seconds())
                print(
                    f"progress done={len(done) + finished} lines_per_hour={rate:.1f} "
                    f"eta_s={eta:.1f} rss_bytes={rss} rss_frac={frac:.3f}",
                    flush=True,
                )
    finally:
        for _ in workers:
            job_q.put(None)
        deadline = time.perf_counter() + 5
        for proc in workers:
            proc.join(timeout=max(0.1, deadline - time.perf_counter()))
            if proc.is_alive():
                proc.terminate()
        handle.close()
    elapsed = max(time.perf_counter() - clock, 1e-6)
    rate = finished / elapsed * 3600.0
    print(
        f"stopped finished={finished} lines_per_hour={rate:.1f} "
        f"max_rss_bytes={max_rss} max_rss_frac={max_frac:.3f}",
        flush=True,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, required=True)
    parser.add_argument("--pass", dest="pass_n", type=int, choices=(1, 2), required=True)
    parser.add_argument("--until", required=True, help="HH:MM local time")
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=None, help="Stop after this many mutants of this pass, including ones already on disk")
    args = parser.parse_args()
    if args.workers < 1:
        parser.error("--workers must be >= 1")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
