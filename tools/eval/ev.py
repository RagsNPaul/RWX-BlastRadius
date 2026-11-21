#!/usr/bin/env python3
"""Score selector/main.py on the sample answers and on bank ground truth.

  python3 tools/eval/ev.py --sqlglot sqlglot --python python3 \
      --tests sample/tests.json --bank bank --maps maps

--tests lives next to answers.json and commits/. Bank ground truth is every
bank/<id>.json plus bank/truth.json (a per-patch file wins if both name an id).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "selector"))

from pack import pack  # noqa: E402
from patch import parse  # noqa: E402
from rules import Maps, select  # noqa: E402
from score import (  # noqa: E402
    MISS_PENALTY_MILLISECONDS,
    _duration_milliseconds,
    _format_milliseconds,
    evaluate_run,
)

REASON = re.compile(r"^(\S+) (\d+) tests (\d+\.\d+)s (.*)$")
SLOW_SECONDS = 3.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlglot", required=True, type=Path, help="checkout the selector parses")
    parser.add_argument("--python", required=True, help="interpreter used to run selector/main.py")
    parser.add_argument("--tests", required=True, type=Path, help="sample/tests.json")
    parser.add_argument("--bank", required=True, type=Path, help="patches and ground-truth json")
    parser.add_argument("--maps", required=True, type=Path, help="maps/ from the pinned commit")
    return parser.parse_args()


def durations_of(tests: list[dict]) -> list[int]:
    durations = [0] * len(tests)
    for test in tests:
        durations[test["number"]] = _duration_milliseconds(test["seconds"])
    return durations


def packing_cost(workers: list[list[int]], durations: list[int]) -> int:
    occupied = [worker for worker in workers if worker]
    if not occupied:
        return 0
    busiest = max(sum(durations[test] for test in worker) for worker in occupied)
    return busiest + 2000 * len(occupied)


def oracle_cost(failing: list[int], durations: list[int]) -> int:
    return packing_cost(pack(failing, durations), durations)


def as_answer(value) -> dict:
    if isinstance(value, dict) and "failing" in value:
        return {"failing": list(value["failing"])}
    if isinstance(value, list):
        return {"failing": list(value)}
    raise ValueError(f"ground truth is not a failing list: {value!r}"[:160])


def load_bank(bank: Path) -> dict[str, dict]:
    """Per-patch json overlaid on truth.json. One entry per commit."""
    answers: dict[str, dict] = {}
    combined = bank / "truth.json"
    if combined.is_file():
        for commit, value in json.loads(combined.read_text()).items():
            answers[commit] = as_answer(value)
    for path in sorted(bank.glob("*.json")):
        if path.name == "truth.json":
            continue
        loaded = json.loads(path.read_text())
        if isinstance(loaded, dict) and "failing" not in loaded and path.stem not in loaded:
            continue
        incoming = as_answer(loaded)
        previous = answers.get(path.stem)
        if previous is not None and previous["failing"] != incoming["failing"]:
            print(f"ev: {path.stem} json differs from truth.json; using the json", file=sys.stderr)
        answers[path.stem] = incoming
    return answers


def selector_log(stderr: str) -> tuple[dict[str, str], float | None]:
    reasons: dict[str, str] = {}
    cpu = None
    for line in stderr.splitlines():
        if line.startswith("cpu_seconds "):
            cpu = float(line.split()[1])
            continue
        match = REASON.match(line)
        if match:
            reasons[match.group(1)] = match.group(4)
    return reasons, cpu


def run_selector(python: str, commits: Path, tests: Path, sqlglot: Path, maps: Path) -> tuple[str, str, int]:
    proc = subprocess.run(
        [
            python,
            str(ROOT / "selector" / "main.py"),
            "--commits", str(commits),
            "--tests", str(tests),
            "--src", str(sqlglot),
            "--maps", str(maps),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.stdout, proc.stderr, proc.returncode


def keep(stdout: str, answers: dict) -> str:
    kept = []
    for line in stdout.splitlines():
        try:
            commit = json.loads(line)["commit"]
        except (json.JSONDecodeError, KeyError, TypeError):
            kept.append(line)
            continue
        if commit in answers:
            kept.append(line)
    return "\n".join(kept) + ("\n" if kept else "")


def load_output(stdout: str) -> dict[str, list]:
    rows = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        value = json.loads(line)
        rows[value["commit"]] = value["workers"]
    return rows


def report(name: str, stdout: str, stderr: str, tests: list[dict], answers: dict, durations: list[int]) -> int:
    reasons, cpu = selector_log(stderr)
    text = stdout if stdout.endswith("\n") or stdout == "" else stdout + "\n"
    result = evaluate_run(text.encode(), tests, answers)
    count = len(answers)
    print(f"{name}:")
    if not result["ok"]:
        where = "" if result["line"] is None else f" on line {result['line']}"
        print(f"  rejected {result['code']}{where}")
        if cpu is not None:
            print(f"  cpu {_format_cpu(cpu)} s")
        return 1

    simulated = result["simulated_milliseconds"]
    missed = result["missed_commits"]
    per_commit = simulated / count / 1000 if count else 0.0
    miss_text = (" " + " ".join(missed)) if missed else ""
    print(
        f"  commits {count}  simulated {_format_milliseconds(simulated)} s"
        f"  {per_commit:.3f} s/commit  misses {len(missed)}{miss_text}"
        f"  cpu {_format_cpu(cpu)} s"
    )
    output = load_output(stdout)
    by_id = {test["number"]: test["id"] for test in tests}
    for commit in missed:
        selected = {test for worker in output.get(commit, []) for test in worker}
        left_out = [number for number in answers[commit]["failing"] if number not in selected]
        shown = ", ".join(by_id.get(number, str(number)) for number in left_out[:8])
        more = f"  +{len(left_out) - 8} more" if len(left_out) > 8 else ""
        print(f"  missed {commit}  {len(left_out)} tests  {shown}{more}")

    ranked = []
    for commit, answer in answers.items():
        cost = packing_cost(output.get(commit, []), durations)
        best = oracle_cost(answer["failing"], durations)
        ranked.append((cost - best, cost, best, commit))
    ranked.sort(key=lambda row: (-row[0], row[3]))
    print("  top 10 by cost − best:")
    for gap, cost, best, commit in ranked[:10]:
        print(
            f"    {commit}  {_format_signed(gap)} s"
            f"  cost {_format_milliseconds(cost)} s"
            f"  best {_format_milliseconds(best)} s"
            f"  {reasons.get(commit, '?')}"
        )
    return 1 if missed else 0


def _leaf(test_id: str) -> str:
    return test_id.rsplit("::", 1)[-1]


def _cause_key(rule: str) -> tuple:
    if rule.startswith("all:"):
        return (0, rule)
    order = {
        "blind": 1,
        "import->file": 2,
        "line": 3,
        "class": 4,
        "word": 5,
        "data": 6,
        "docstring": 7,
        "py-read": 8,
        "testfile": 9,
    }
    return (order.get(rule, 10), rule)


def _explain(patch: Path, maps: Maps) -> tuple[set, list]:
    """Same selection as main.py, plus (rule, line-range, tests) notes. Notes are not selected."""
    try:
        text = patch.read_text(encoding="utf-8")
        files = parse(text)
        if text.strip() and not files:
            raise ValueError("no file found in the patch")
        blame: list = []
        selected, _reasons = select(files, maps, blame)
    except Exception:
        return set(maps.everything), [("all:error", patch.name, maps.everything)]
    return set(selected), blame


def _causes_for(blame: list) -> dict[tuple[str, str], set]:
    causes: dict[tuple[str, str], set] = {}
    for rule, where, numbers in blame:
        causes.setdefault((rule, where), set()).update(numbers)
    return causes


def expensive_report(groups, tests: list[dict], sqlglot: Path, maps_dir: Path) -> int:
    """Selected tests over 3s, the rule and line that added each, and whether it failed.

    `groups` is (origin, commits dir, answers, selector workers) for sample and bank.
    Waste is seconds of a test that was selected and passed. Returns 1 when the traced
    selection disagrees with the selector process.
    """
    slow = [test for test in tests if test["seconds"] > SLOW_SECONDS]
    slow.sort(key=lambda test: -test["seconds"])
    by_number = {test["number"]: test for test in slow}
    maps = Maps(str(maps_dir), str(sqlglot))
    maps.check(tests)

    rows = []
    quiet = []
    mismatches = []
    pair_seconds: dict[tuple[str, str], float] = {}
    pair_commits: dict[tuple[str, str], set] = {}
    pair_counts: dict[tuple[str, str], dict[int, int]] = {}
    rule_seconds: dict[str, float] = {}
    rule_commits: dict[str, set] = {}
    unique_waste = 0.0

    for origin, commits, answers, output in groups:
        for commit in sorted(answers):
            patch = commits / f"{commit}.patch"
            official = {number for worker in output.get(commit, []) for number in worker}
            if not patch.is_file():
                traced, blame = set(), []
            else:
                traced, blame = _explain(patch, maps)
            if traced != official:
                mismatches.append(f"{origin}:{commit}")
            causes = _causes_for(blame)
            picked = []
            waste = failed = 0.0
            for number, test in by_number.items():
                if number not in official:
                    continue
                passed = number not in answers[commit]["failing"]
                seconds = test["seconds"]
                if passed:
                    waste += seconds
                else:
                    failed += seconds
                why = sorted(
                    ((rule, where) for (rule, where), numbers in causes.items() if number in numbers),
                    key=lambda item: (_cause_key(item[0]), item[1]),
                )
                picked.append((0.0 if not passed else seconds, seconds, passed, test, why))
                if not passed:
                    continue
                for rule in {rule for rule, _where in why}:
                    rule_seconds[rule] = rule_seconds.get(rule, 0.0) + seconds
                    rule_commits.setdefault(rule, set()).add(f"{origin}:{commit}")
                for rule, where in why:
                    pair = (rule, where)
                    pair_seconds[pair] = pair_seconds.get(pair, 0.0) + seconds
                    pair_commits.setdefault(pair, set()).add(f"{origin}:{commit}")
                    counts = pair_counts.setdefault(pair, {})
                    counts[number] = counts.get(number, 0) + 1
            if not picked:
                quiet.append(f"{origin}:{commit}")
                continue
            unique_waste += waste
            picked.sort(key=lambda item: (-item[0], -item[1], item[3]["id"]))
            rows.append((waste, failed, origin, commit, picked))

    rows.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    print("expensive (>3s):")
    print("  wasted = selected and passed. Each test is listed under every rule that added it.")
    if mismatches:
        print(f"  trace disagrees with the selector on {len(mismatches)}: {' '.join(mismatches[:8])}")
    for waste, failed, origin, commit, picked in rows:
        failed_text = f"  failed {failed:.3f}s" if failed else ""
        print(f"  {origin} {commit}  wasted {waste:.3f}s{failed_text}")
        for _waste, seconds, passed, test, why in picked:
            mark = "pass" if passed else "FAIL"
            print(f"    {seconds:7.3f}s {mark}  {test['id']}")
            shown = why[:8]
            for rule, where in shown:
                print(f"      {rule}  {where}")
            if len(why) > len(shown):
                print(f"      +{len(why) - len(shown)} more")
            if not why:
                print("      unattributed")
    if quiet:
        print(f"  no selected test over 3s: {len(quiet)} commits")

    print("top 10 (rule, line) by seconds selected but passed:")
    ranked = sorted(pair_seconds, key=lambda pair: (-pair_seconds[pair], pair[0], pair[1]))
    if not ranked:
        print("    none")
    for pair in ranked[:10]:
        rule, where = pair
        counts = pair_counts[pair]
        parts = []
        for number, test in by_number.items():
            count = counts.get(number, 0)
            if count:
                parts.append(f"{_leaf(test['id'])} {test['seconds']:.3f}s x {count}")
        n_commits = len(pair_commits[pair])
        commit_word = "commit" if n_commits == 1 else "commits"
        print(
            f"    {pair_seconds[pair]:.3f}s  {n_commits} {commit_word}"
            f"  {rule}  {where}"
        )
        print(f"      {', '.join(parts)}")
    if len(ranked) > 10:
        floor = pair_seconds[ranked[9]]
        tied = sum(1 for pair in ranked[10:] if pair_seconds[pair] == floor)
        if tied:
            print(f"  {tied} more pairs tie with the last at {floor:.3f}s")
    print("by rule (a passing test counted once per rule, even if several lines used it):")
    for rule, seconds in sorted(rule_seconds.items(), key=lambda item: (-item[1], item[0])):
        n_commits = len(rule_commits[rule])
        commit_word = "commit" if n_commits == 1 else "commits"
        print(f"    {seconds:.3f}s  {n_commits} {commit_word}  {rule}")
    print(f"  wasted {unique_waste:.3f}s in all, each passing test counted once")
    return 1 if mismatches else 0


def _format_cpu(cpu: float | None) -> str:
    if cpu is None:
        return "missing"
    return f"{cpu:.3f}"


def _format_signed(milliseconds: int) -> str:
    sign = "+" if milliseconds >= 0 else "-"
    return sign + _format_milliseconds(abs(milliseconds))


def main() -> int:
    args = parse_args()
    sample = args.tests.parent
    answers_path = sample / "answers.json"
    commits = sample / "commits"
    if not answers_path.is_file():
        print(f"ev: no answers.json next to {args.tests}", file=sys.stderr)
        return 1
    tests = json.loads(args.tests.read_text())["tests"]
    durations = durations_of(tests)
    sample_answers = json.loads(answers_path.read_text())["commits"]
    bank_answers = load_bank(args.bank)
    if not bank_answers:
        print(f"ev: no ground truth in {args.bank}", file=sys.stderr)
        return 1

    status = 0
    groups = []
    sample_out, sample_err, sample_rc = run_selector(
        args.python, commits, args.tests, args.sqlglot, args.maps
    )
    if sample_rc:
        print(sample_err, file=sys.stderr)
        print(f"ev: selector exited {sample_rc} on sample", file=sys.stderr)
        status = 1
    else:
        status |= report("sample", sample_out, sample_err, tests, sample_answers, durations)
        groups.append(("sample", commits, sample_answers, load_output(sample_out)))

    bank_out, bank_err, bank_rc = run_selector(
        args.python, args.bank, args.tests, args.sqlglot, args.maps
    )
    if bank_rc:
        print(bank_err, file=sys.stderr)
        print(f"ev: selector exited {bank_rc} on bank", file=sys.stderr)
        status = 1
    else:
        kept = keep(bank_out, bank_answers)
        status |= report("bank", kept, bank_err, tests, bank_answers, durations)
        groups.append(("bank", args.bank, bank_answers, load_output(kept)))

    if groups:
        status |= expensive_report(groups, tests, args.sqlglot, args.maps)
    return status


if __name__ == "__main__":
    sys.exit(main())
