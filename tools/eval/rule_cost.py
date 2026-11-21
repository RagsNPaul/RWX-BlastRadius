#!/usr/bin/env python3
"""Seconds each rule adds beyond the true failing set, then one flag at a time.

  python3 tools/eval/rule_cost.py --sqlglot sqlglot --python .venv/bin/python \
      --tests sample/tests.json --bank bank --maps maps

The five columns are packing seconds of tests that rule alone added and that did
not fail. The flag section turns each flag on by itself and prints
net = holdout packing saved − 300 × extra holdout misses.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "eval"))

import ev  # noqa: E402
from pack import pack  # noqa: E402
from patch import parse  # noqa: E402
from rules import Maps, select  # noqa: E402

RULES = ("line", "class", "word", "neighbor", "dialect")
FLAGS = (
    "WORD_OFF",
    "WORD_NO_EXPENSIVE",
    "NEIGHBOR_OFF",
    "DIALECT_FILE_OFF",
    "CLASS_CAP",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlglot", required=True, type=Path)
    parser.add_argument("--python", required=True)
    parser.add_argument("--tests", required=True, type=Path)
    parser.add_argument("--bank", required=True, type=Path)
    parser.add_argument("--maps", required=True, type=Path)
    parser.add_argument("--holdout", type=Path)
    return parser.parse_args()


def _exclusive(selected: set, blame: list, failing: set) -> dict[str, set]:
    by_rule = {name: set() for name in RULES}
    others: set = set()
    for rule, _where, tests in blame:
        numbers = tests if isinstance(tests, set) else set(tests)
        if rule in by_rule:
            by_rule[rule] |= numbers
        else:
            others |= numbers
    exclusive = {}
    for name in RULES:
        rest = set(others)
        rest |= failing
        for other, numbers in by_rule.items():
            if other != name:
                rest |= numbers
        exclusive[name] = (by_rule[name] & selected) - rest
    return exclusive


def _pack(numbers: set, durations: list[int]) -> int:
    return ev.packing_cost(pack(sorted(numbers), durations), durations)


def _baseline_env() -> None:
    os.environ["IMPORT_CHECK"] = "1"
    os.environ["EXPENSIVE_CHECK"] = "0"
    for name in FLAGS:
        os.environ[name] = "0"


def _report_set(name: str, commits: dict[str, Path], answers: dict, maps: Maps, durations: list[int]) -> dict:
    totals = {rule: 0 for rule in RULES}
    totals["word1s"] = 0
    print(f"{name}:")
    print(f"  {'commit':<22} {'line':>8} {'class':>8} {'word':>8} {'neighbor':>8} {'dialect':>8}")
    for commit in sorted(answers):
        patch = commits[commit]
        failing = set(answers[commit]["failing"])
        try:
            files = parse(patch.read_text(encoding="utf-8"))
            blame: list = []
            selected, _reasons = select(files, maps, blame)
            selected = set(selected)
        except Exception:
            selected, blame = set(maps.everything), []
        extra = _exclusive(selected, blame, failing)
        costs = {}
        full = _pack(selected, durations)
        for rule in RULES:
            costs[rule] = full - _pack(selected - extra[rule], durations)
            totals[rule] += costs[rule]
        over = {number for number in extra["word"] if durations[number] > 1000}
        totals["word1s"] += full - _pack(selected - over, durations)
        print(
            f"  {commit:<22} "
            + " ".join(f"{costs[rule] / 1000:8.3f}" for rule in RULES)
        )
    commits_n = len(answers)
    print(
        f"  {'total':<22} "
        + " ".join(f"{totals[rule] / 1000:8.3f}" for rule in RULES)
        + f"   word>1s {totals['word1s'] / 1000:.3f}   commits {commits_n}"
    )
    return totals


def _score(stdout: str, answers: dict, durations: list[int]) -> tuple[float, list[str]]:
    workers = ev.load_output(stdout)
    packing = 0
    missed = []
    for commit, answer in answers.items():
        chosen = workers.get(commit, [])
        selected = {test for worker in chosen for test in worker}
        packing += ev.packing_cost(chosen, durations)
        if set(answer["failing"]) - selected:
            missed.append(commit)
    return packing / 1000, missed


def _run_flag(flag: str | None, args, sample_answers, hold_answers, durations, hold_dir: Path):
    _baseline_env()
    if flag:
        os.environ[flag] = "1"
    sample_dir = args.tests.parent / "commits"
    out, err, code = ev.run_selector(args.python, sample_dir, args.tests, args.sqlglot, args.maps)
    if code:
        sys.stderr.write(err[-1500:])
        raise SystemExit(f"rule_cost: selector exited {code}")
    _reasons, sample_cpu = ev.selector_log(err)
    sample_s, sample_miss = _score(out, sample_answers, durations)
    out, err, code = ev.run_selector(args.python, hold_dir, args.tests, args.sqlglot, args.maps)
    if code:
        sys.stderr.write(err[-1500:])
        raise SystemExit(f"rule_cost: selector exited {code}")
    _reasons, hold_cpu = ev.selector_log(err)
    hold_s, hold_miss = _score(out, hold_answers, durations)
    return {
        "sample_s": sample_s,
        "sample_miss": sample_miss,
        "hold_s": hold_s,
        "hold_miss": hold_miss,
        "cpu": (sample_cpu or 0.0) + (hold_cpu or 0.0),
    }


def main() -> int:
    args = parse_args()
    _baseline_env()
    tests = json.loads(args.tests.read_text())["tests"]
    durations = ev.durations_of(tests)
    maps = Maps(str(args.maps), str(args.sqlglot))
    maps.check(tests)
    sample_dir = args.tests.parent
    sample_answers = json.loads((sample_dir / "answers.json").read_text())["commits"]
    bank_answers = ev.load_bank(args.bank)
    hold_path = args.holdout or (args.bank / "holdout.txt")
    hold_ids = [line.strip() for line in hold_path.read_text().splitlines() if line.strip()]
    hold_answers = {commit: bank_answers[commit] for commit in hold_ids}
    sample_commits = {commit: sample_dir / "commits" / f"{commit}.patch" for commit in sample_answers}
    bank_commits = {commit: args.bank / f"{commit}.patch" for commit in bank_answers}
    hold_commits = {commit: bank_commits[commit] for commit in hold_ids}

    print("packing seconds added beyond the failing set")
    sample_totals = _report_set("sample", sample_commits, sample_answers, maps, durations)
    hold_totals = _report_set("holdout", hold_commits, hold_answers, maps, durations)
    bank_totals = _report_set("bank", bank_commits, bank_answers, maps, durations)
    rank = {
        "WORD_OFF": hold_totals["word"],
        "WORD_NO_EXPENSIVE": hold_totals["word1s"],
        "NEIGHBOR_OFF": hold_totals["neighbor"],
        "DIALECT_FILE_OFF": hold_totals["dialect"],
        "CLASS_CAP": hold_totals["class"],
    }
    order = sorted(FLAGS, key=lambda name: (-rank[name], name))
    print("flag order by holdout waste: " + ", ".join(f"{name} {rank[name] / 1000:.3f}s" for name in order))
    print(
        "bank waste: "
        + ", ".join(
            f"{name} {({'WORD_OFF': bank_totals['word'], 'WORD_NO_EXPENSIVE': bank_totals['word1s'], 'NEIGHBOR_OFF': bank_totals['neighbor'], 'DIALECT_FILE_OFF': bank_totals['dialect'], 'CLASS_CAP': bank_totals['class']})[name] / 1000:.3f}s"
            for name in order
        )
    )

    with tempfile.TemporaryDirectory(prefix="rule-holdout-") as tmp:
        dest = Path(tmp)
        for commit in hold_ids:
            (dest / f"{commit}.patch").symlink_to(bank_commits[commit].resolve())
        base = _run_flag(None, args, sample_answers, hold_answers, durations, dest)
        print(
            f"baseline sample {base['sample_s']:.3f}s misses {len(base['sample_miss'])}  "
            f"holdout {base['hold_s']:.3f}s misses {len(base['hold_miss'])}  cpu {base['cpu']:.3f}"
        )
        print(f"{'flag':<20} {'sample_s':>10} {'smiss':>5} {'hold_s':>10} {'hmiss':>5} {'saved':>8} {'net':>8}")
        for name in order:
            row = _run_flag(name, args, sample_answers, hold_answers, durations, dest)
            saved = base["hold_s"] - row["hold_s"]
            extra = len(row["hold_miss"]) - len(base["hold_miss"])
            net = saved - 300 * extra
            print(
                f"{name:<20} {row['sample_s']:10.3f} {len(row['sample_miss']):5d} "
                f"{row['hold_s']:10.3f} {len(row['hold_miss']):5d} {saved:8.3f} {net:8.3f}"
            )
            if row["sample_miss"]:
                print(f"  sample misses: {' '.join(row['sample_miss'])}")
            if row["hold_miss"]:
                print(f"  holdout misses: {' '.join(row['hold_miss'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
