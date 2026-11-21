#!/usr/bin/env python3
"""Run the selector on sample/ and bank/, score both, append one line to RESULTS.md.

Exits 1 on a miss or a rejected output, 2 when a bank patch is still waiting for ground truth.
The simulated seconds are score.py's, penalties included.
"""
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from score import (  # noqa: E402
    MISS_PENALTY_MILLISECONDS,
    _duration_milliseconds,
    _format_milliseconds,
    evaluate_run,
)

RESULTS = ROOT / "RESULTS.md"
HEADER = (
    "# Results\n\n"
    "| date | git | sample s | sample misses | bank | bank misses | bank s | cpu s | board s |\n"
    "| --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
)
LINE = re.compile(r"^(\S+) (\d+) tests (\d+\.\d+)s (.*)$")


def run_selector(args):
    proc = subprocess.run(
        [sys.executable, str(ROOT / "selector" / "main.py"), *args],
        cwd=ROOT, capture_output=True,
    )
    stdout, stderr = proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace")
    if proc.returncode:
        print(f"evaluate: selector exited {proc.returncode}\n{stderr}", file=sys.stderr)
    return stdout, stderr


def selector_log(stderr):
    rows, cpu = {}, None
    for line in stderr.splitlines():
        if line.startswith("cpu_seconds "):
            cpu = float(line.split()[1])
            continue
        match = LINE.match(line)
        if match:
            rows[match.group(1)] = match.group(4)
    return rows, cpu


def durations_of(tests):
    durations = [0] * len(tests)
    for test in tests:
        durations[test["number"]] = _duration_milliseconds(test["seconds"])
    return durations


def commit_cost(workers, durations):
    occupied = [worker for worker in workers if worker]
    if not occupied:
        return 0
    busiest = max(sum(durations[test] for test in worker) for worker in occupied)
    return busiest + 2000 * len(occupied)


def tags_of(reason):
    parts = reason.split(",")
    if all(" " not in part and part for part in parts):
        return parts
    return [reason]


def rule_table(reasons, costs):
    files, commits, cost = defaultdict(int), defaultdict(int), defaultdict(int)
    for commit, reason in reasons.items():
        seen = set()
        for tag in tags_of(reason):
            files[tag] += 1
            if tag not in seen:
                commits[tag] += 1
                cost[tag] += costs.get(commit, 0)
                seen.add(tag)
    return files, commits, cost


def print_rules(reasons, costs):
    files, commits, cost = rule_table(reasons, costs)
    print(f"  {'rule':<28} {'files':>6} {'commits':>8} {'cost_s':>10}")
    for tag in sorted(files, key=lambda tag: (-commits[tag], tag)):
        print(f"  {tag:<28} {files[tag]:>6} {commits[tag]:>8} {cost[tag] / 1000:>10.3f}")


def load_output(stdout):
    rows = {}
    for line in stdout.splitlines():
        value = json.loads(line)
        rows[value["commit"]] = value["workers"]
    return rows


def score_stdout(stdout, tests, answers):
    text = stdout if stdout.endswith("\n") or stdout == "" else stdout + "\n"
    return evaluate_run(text.encode(), tests, answers)


def report(name, stdout, stderr, tests, answers):
    reasons, cpu = selector_log(stderr)
    result = score_stdout(stdout, tests, answers)
    print(f"{name}:")
    if not result["ok"]:
        where = "" if result["line"] is None else f" on line {result['line']}"
        print(f"  Rejected: {result['code']}{where}")
        return result, reasons, cpu, {}
    output = load_output(stdout)
    durations = durations_of(tests)
    costs = {commit: commit_cost(output[commit], durations) for commit in answers}
    by_id = {test["number"]: test["id"] for test in tests}
    missed = result["missed_commits"]
    penalty = MISS_PENALTY_MILLISECONDS * len(missed)
    print(
        f"  commits {len(answers)}  simulated {_format_milliseconds(result['simulated_milliseconds'])} s"
        f"  misses {len(missed)} ({_format_milliseconds(penalty)} s)"
    )
    for commit in missed:
        selected = {test for worker in output[commit] for test in worker}
        left_out = [number for number in answers[commit]["failing"] if number not in selected]
        ids = ", ".join(by_id[number] for number in left_out)
        print(f"  missed {commit}  rule {reasons.get(commit, '?')}  {ids}")
    print_rules(reasons, costs)
    quiet = [commit for commit, answer in answers.items() if not answer["failing"] and costs[commit] > 0]
    if quiet:
        print(f"  break nothing but cost > 0: {len(quiet)}")
        for commit in sorted(quiet, key=lambda commit: -costs[commit])[:12]:
            print(f"    {commit}  {costs[commit] / 1000:.3f}s  {reasons.get(commit, '?')}")
    if cpu is None:
        print("  cpu_seconds missing from selector stderr")
    else:
        print(f"  cpu_seconds {cpu:.3f}")
    return result, reasons, cpu, costs


def bank_answers():
    """Patches with a json are scored. The rest are waiting for ground truth."""
    answers, waiting = {}, []
    bank = ROOT / "bank"
    if not bank.is_dir():
        return answers, waiting
    for patch in sorted(bank.glob("*.patch")):
        truth = bank / f"{patch.stem}.json"
        if not truth.exists():
            waiting.append(patch.stem)
            continue
        answers[patch.stem] = json.loads(truth.read_text())
    return answers, waiting


def keep_answered(stdout, answers, waiting):
    waiting = set(waiting)
    kept = []
    for line in stdout.splitlines():
        try:
            commit = json.loads(line)["commit"]
        except (json.JSONDecodeError, KeyError, TypeError):
            kept.append(line)
            continue
        if commit not in waiting:
            kept.append(line)
    return "\n".join(kept) + ("\n" if kept else "")


def git_mark():
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
    except OSError:
        return "unknown"
    mark = head.stdout.strip() or "unknown"
    if dirty.stdout.strip():
        mark += " dirty"
    return mark


def append_result(sample, bank, cpu):
    if not RESULTS.exists():
        RESULTS.write_text(HEADER)
    sample_s = _format_milliseconds(sample["simulated_milliseconds"]) if sample and sample["ok"] else "rejected"
    sample_m = len(sample["missed_commits"]) if sample and sample["ok"] else "-"
    if bank is None:
        bank_n, bank_m, bank_s = "-", "-", "-"
    elif not bank["ok"]:
        bank_n, bank_m, bank_s = "-", "-", "rejected"
    else:
        bank_n, bank_m = bank["scored"], len(bank["missed_commits"])
        bank_s = _format_milliseconds(bank["simulated_milliseconds"])
    cpu_s = "-" if cpu is None else f"{cpu:.3f}"
    with RESULTS.open("a") as f:
        f.write(
            f"| {date.today().isoformat()} | {git_mark()} | {sample_s} | {sample_m} "
            f"| {bank_n} | {bank_m} | {bank_s} | {cpu_s} | |\n"
        )


def main():
    tests = json.loads((ROOT / "sample" / "tests.json").read_text())["tests"]
    sample_answers = json.loads((ROOT / "sample" / "answers.json").read_text())["commits"]
    sample_out, sample_err = run_selector(["--data", str(ROOT / "sample")])
    sample = report("sample", sample_out, sample_err, tests, sample_answers)[0]
    _, sample_cpu = selector_log(sample_err)

    answers, waiting = bank_answers()
    bank, bank_cpu = None, None
    if not answers and not waiting:
        print("bank: no mutants")
    else:
        if waiting:
            print(f"bank: waiting for ground truth ({len(waiting)}): {', '.join(waiting)}")
        if answers:
            bank_out, bank_err = run_selector(["--commits", str(ROOT / "bank"), "--tests", str(ROOT / "sample" / "tests.json")])
            bank_result, _, bank_cpu, _ = report(
                "bank", keep_answered(bank_out, answers, waiting), bank_err, tests, answers
            )
            bank_result["scored"] = len(answers) if bank_result["ok"] else 0
            bank = bank_result
    cpus = [cpu for cpu in (sample_cpu, bank_cpu) if cpu is not None]
    total_cpu = sum(cpus) if cpus else None
    if total_cpu is not None:
        print(f"selector cpu_seconds {total_cpu:.3f}")
    append_result(sample, bank, total_cpu)

    if not sample["ok"] or (bank is not None and not bank["ok"]):
        return 1
    if sample["missed_commits"] or (bank is not None and bank["missed_commits"]):
        return 1
    if waiting or bank is None:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
