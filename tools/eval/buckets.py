#!/usr/bin/env python3
"""Split each commit's selector cost into best, fallback, expensive waste, and other.

  python3 tools/eval/buckets.py --sqlglot sqlglot --python .venv/bin/python \
      --tests sample/tests.json --bank bank --maps maps

Known corrections are written first: r8_enumcmp_08 fails nothing, r8_copyflag_02
fails only test 1551. Any remaining truth that lists test_max_nodes or
test_execute_tpcds is remeasured with gt.py --timeout=600 -n 4.

Cost of one commit (its packed workers, in ms) splits as:
  best        packing cost of the true failing set
  fallback    if the reason contains "all:", cost minus best; otherwise 0
  expensive   if not a fallback, cost minus the repack without passing tests over 3s
  other       cost minus those three, so the four numbers add up to the cost

Holdout ids are frozen in bank/holdout.txt (30%, seed 0). An existing file is kept.
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "selector"))
sys.path.insert(0, str(ROOT / "tools" / "eval"))

import ev  # noqa: E402
from rules import Maps  # noqa: E402
from score import _format_milliseconds  # noqa: E402

SLOW_SECONDS = 3.0
# The expensive tests, not the 1 ms dialect test that shares the name test_max_nodes.
SLOW_IDS = (
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
)
GT_IMAGE = "blast-radius-gt:latest"
CORRECTIONS = {
    "r8_enumcmp_08": [],
    "r8_copyflag_02": [1551],
}
HOLDOUT_FRACTION = 0.30
HOLDOUT_SEED = 0
GT_TIMEOUT = "600"
GT_WORKERS = "4"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlglot", required=True, type=Path)
    parser.add_argument("--python", required=True)
    parser.add_argument("--tests", required=True, type=Path)
    parser.add_argument("--bank", required=True, type=Path)
    parser.add_argument("--maps", required=True, type=Path)
    return parser.parse_args()


def write_answer(path: Path, failing: list[int]) -> None:
    path.write_text(json.dumps({"failing": failing}, separators=(",", ":")) + "\n")


def apply_corrections(bank: Path) -> None:
    for commit, failing in CORRECTIONS.items():
        write_answer(bank / f"{commit}.json", failing)
        print(f"buckets: corrected {commit} -> {failing}", file=sys.stderr)


def slow_numbers(tests: list[dict]) -> set[int]:
    return {test["number"] for test in tests if test["id"] in SLOW_IDS}


def refresh_slow_truths(args: argparse.Namespace, tests: list[dict]) -> int:
    """Rerun gt.py for bank truths that still list a slow leaf. Returns the exit code."""
    marked = slow_numbers(tests)
    answers = ev.load_bank(args.bank)
    stale = sorted(
        commit
        for commit, answer in answers.items()
        if marked.intersection(answer["failing"]) and (args.bank / f"{commit}.patch").is_file()
    )
    if not stale:
        print("buckets: no truth still lists test_max_nodes or test_execute_tpcds", file=sys.stderr)
        return 0
    print(
        f"buckets: rerunning gt.py --timeout={GT_TIMEOUT} -n {GT_WORKERS} for {' '.join(stale)}",
        file=sys.stderr,
    )
    backups: list[tuple[Path, str]] = []
    for commit in stale:
        path = args.bank / f"{commit}.json"
        if path.is_file():
            backups.append((path, path.read_text()))
            path.unlink()
    # The pinned image, not the host interpreter: a different SQLite would record the wrong failures.
    bank = args.bank.resolve()
    proc = subprocess.run(
        [
            "docker", "run", "--rm",
            "--platform", "linux/arm64",
            "--network", "none",
            "--cpuset-cpus", "0-3",
            "-v", f"{ROOT}:/work:ro",
            "-v", f"{bank}:/bank",
            "-e", "PYTHONDONTWRITEBYTECODE=1",
            GT_IMAGE,
            "python", "/work/tools/eval/gt.py",
            "--sqlglot", "/work/sqlglot",
            "--python", "python",
            "--tests", "/work/sample/tests.json",
            "--bank", "/bank",
            "--timeout", GT_TIMEOUT,
            "-n", GT_WORKERS,
            *[f"/bank/{commit}.patch" for commit in stale],
        ],
        cwd=ROOT,
    )
    if proc.returncode:
        for path, text in backups:
            if not path.is_file():
                path.write_text(text)
        print(f"buckets: gt.py exited {proc.returncode}; restored the previous truths", file=sys.stderr)
    return proc.returncode


def freeze_holdout(bank: Path) -> list[str]:
    path = bank / "holdout.txt"
    if path.is_file():
        ids = [line.strip() for line in path.read_text().splitlines() if line.strip()]
        print(f"buckets: holdout kept ({len(ids)} ids in {path.name})", file=sys.stderr)
        return ids
    population = sorted(patch.stem for patch in bank.glob("*.patch"))
    count = int(len(population) * HOLDOUT_FRACTION + 0.5)
    chosen = sorted(random.Random(HOLDOUT_SEED).sample(population, count))
    path.write_text("".join(f"{commit}\n" for commit in chosen))
    print(
        f"buckets: froze {len(chosen)} of {len(population)} bank ids"
        f" (seed {HOLDOUT_SEED}) into {path.name}",
        file=sys.stderr,
    )
    return chosen


def _primary_rule(number: int, causes: dict) -> str:
    why = sorted(
        (rule for (rule, _where), numbers in causes.items() if number in numbers),
        key=ev._cause_key,
    )
    return why[0] if why else "unattributed"


def classify(commit: str, workers: list, failing: list[int], reason: str, durations: list[int], tests: list[dict], causes: dict) -> dict:
    cost = ev.packing_cost(workers, durations)
    best = ev.oracle_cost(failing, durations)
    selected = {number for worker in workers for number in worker}
    failed = set(failing)
    by_id = {test["number"]: test for test in tests}
    passing_slow = [
        number
        for number in selected
        if number not in failed and by_id[number]["seconds"] > SLOW_SECONDS
    ]
    if "all:" in reason:
        fallback = cost - best
        expensive = 0
        shares: dict[str, int] = {}
    else:
        fallback = 0
        kept = [number for number in selected if number not in set(passing_slow)]
        reduced = ev.oracle_cost(kept, durations)
        expensive = cost - reduced if reduced < cost else 0
        weights = {}
        for number in passing_slow:
            rule = _primary_rule(number, causes)
            weights[rule] = weights.get(rule, 0) + durations[number]
        shares = _share(expensive, weights)
    other = cost - best - fallback - expensive
    return {
        "cost": cost,
        "best": best,
        "fallback": fallback,
        "expensive": expensive,
        "other": other,
        "shares": shares,
        "miss": any(number not in selected for number in failed),
    }


def _share(total: int, weights: dict[str, int]) -> dict[str, int]:
    if total <= 0 or not weights:
        return {}
    weight_sum = sum(weights.values())
    if weight_sum <= 0:
        return {}
    names = sorted(weights)
    shares = {name: total * weights[name] // weight_sum for name in names}
    shares[names[-1]] += total - sum(shares.values())
    return {name: amount for name, amount in shares.items() if amount}


def tally(rows: list[dict]) -> dict:
    keys = ("best", "fallback", "expensive", "other", "cost")
    totals = {key: sum(row[key] for row in rows) for key in keys}
    shares: dict[str, int] = {}
    for row in rows:
        for rule, amount in row["shares"].items():
            shares[rule] = shares.get(rule, 0) + amount
    totals["shares"] = shares
    totals["misses"] = sum(1 for row in rows if row["miss"])
    totals["commits"] = len(rows)
    return totals


def _rate(milliseconds: int, commits: int) -> float:
    if not commits:
        return 0.0
    return milliseconds / commits / 1000


def print_set(name: str, totals: dict) -> None:
    commits = totals["commits"]
    print(f"{name}: commits {commits}  misses {totals['misses']}")
    for key in ("best", "fallback", "expensive", "other"):
        amount = totals[key]
        print(f"  {key:<10} {_format_milliseconds(amount)} s   {_rate(amount, commits):.3f} s/commit")
    if totals["shares"]:
        print("  expensive by rule:")
        for rule, amount in sorted(totals["shares"].items(), key=lambda item: (-item[1], item[0])):
            print(f"    {rule:<16} {_format_milliseconds(amount)} s   {_rate(amount, commits):.3f} s/commit")
    print(f"  {'total':<10} {_format_milliseconds(totals['cost'])} s   {_rate(totals['cost'], commits):.3f} s/commit")


def score_set(name: str, commits_dir: Path, answers: dict, output: dict, reasons: dict, durations: list[int], tests: list[dict], maps: Maps) -> dict:
    rows = []
    for commit in sorted(answers):
        patch = commits_dir / f"{commit}.patch"
        if patch.is_file():
            _selected, blame = ev._explain(patch, maps)
        else:
            blame = []
        causes = ev._causes_for(blame)
        rows.append(
            classify(
                commit,
                output.get(commit, []),
                answers[commit]["failing"],
                reasons.get(commit, ""),
                durations,
                tests,
                causes,
            )
        )
    totals = tally(rows)
    print_set(name, totals)
    return totals


def main() -> int:
    args = parse_args()
    if not args.tests.is_file():
        print(f"buckets: --tests not found: {args.tests}", file=sys.stderr)
        return 1
    tests = json.loads(args.tests.read_text())["tests"]
    durations = ev.durations_of(tests)
    apply_corrections(args.bank)
    if refresh_slow_truths(args, tests):
        return 1

    sample = args.tests.parent
    sample_answers = json.loads((sample / "answers.json").read_text())["commits"]
    bank_answers = ev.load_bank(args.bank)
    if not bank_answers:
        print(f"buckets: no ground truth in {args.bank}", file=sys.stderr)
        return 1
    holdout_ids = freeze_holdout(args.bank)
    holdout_answers = {commit: bank_answers[commit] for commit in holdout_ids if commit in bank_answers}
    missing = [commit for commit in holdout_ids if commit not in bank_answers]
    if missing:
        print(f"buckets: holdout without truth: {len(missing)}", file=sys.stderr)

    maps = Maps(str(args.maps), str(args.sqlglot))
    maps.check(tests)
    status = 0

    sample_out, sample_err, sample_rc = ev.run_selector(
        args.python, sample / "commits", args.tests, args.sqlglot, args.maps
    )
    if sample_rc:
        print(sample_err, file=sys.stderr)
        print(f"buckets: selector exited {sample_rc} on sample", file=sys.stderr)
        return sample_rc
    sample_reasons, _cpu = ev.selector_log(sample_err)
    sample_output = ev.load_output(sample_out)
    sample_totals = score_set(
        "sample", sample / "commits", sample_answers, sample_output, sample_reasons, durations, tests, maps
    )

    bank_out, bank_err, bank_rc = ev.run_selector(
        args.python, args.bank, args.tests, args.sqlglot, args.maps
    )
    if bank_rc:
        print(bank_err, file=sys.stderr)
        print(f"buckets: selector exited {bank_rc} on bank", file=sys.stderr)
        return bank_rc
    bank_reasons, _cpu = ev.selector_log(bank_err)
    kept = ev.keep(bank_out, bank_answers)
    bank_output = ev.load_output(kept)
    bank_totals = score_set(
        "bank", args.bank, bank_answers, bank_output, bank_reasons, durations, tests, maps
    )
    score_set(
        "holdout", args.bank, holdout_answers, bank_output, bank_reasons, durations, tests, maps
    )

    sample_rate = _rate(sample_totals["cost"], sample_totals["commits"])
    bank_rate = _rate(bank_totals["cost"], bank_totals["commits"])
    board = 40 * (0.6 * sample_rate + 0.4 * bank_rate)
    print(
        f"board: {board:.3f} s"
        f"   40 × (0.6 × {sample_rate:.3f} + 0.4 × {bank_rate:.3f})"
    )
    return status


if __name__ == "__main__":
    sys.exit(main())
