#!/usr/bin/env python3
"""Every on/off mix of the V3 flags, scored on the sample and the holdout.

  python3 tools/eval/tune.py --sqlglot sqlglot --python .venv/bin/python \
      --tests sample/tests.json --bank bank --maps maps

The board estimate is the V3-0 blend, with the holdout standing in for the bank:
40 × (0.6 × sample s/commit + 0.4 × holdout s/commit). Seconds are packing cost.
A miss is a commit that leaves out any failing test. Lower board is better.
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

FLAGS = (
    "IMPORT_CHECK",
    "EXPENSIVE_CHECK",
    "WORD_NO_EXPENSIVE",
    "CLASS_CAP",
    "NEIGHBOR_OFF",
    "DIALECT_FILE_OFF",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlglot", required=True, type=Path)
    parser.add_argument("--python", required=True)
    parser.add_argument("--tests", required=True, type=Path)
    parser.add_argument("--bank", required=True, type=Path)
    parser.add_argument("--maps", required=True, type=Path)
    parser.add_argument("--holdout", type=Path, help="defaults to <bank>/holdout.txt")
    return parser.parse_args()


def _pack_and_misses(workers: dict, answers: dict, durations: list[int]) -> tuple[int, int, list[str]]:
    packing = 0
    missed = []
    for commit, answer in answers.items():
        chosen = workers.get(commit, [])
        selected = {test for worker in chosen for test in worker}
        packing += ev.packing_cost(chosen, durations)
        failing = set(answer["failing"])
        if failing - selected:
            missed.append(commit)
    return packing, len(missed), missed


def _run(python: str, commits: Path, tests: Path, sqlglot: Path, maps: Path, answers: dict, durations: list[int]):
    out, err, code = ev.run_selector(python, commits, tests, sqlglot, maps)
    if code:
        sys.stderr.write(err[-2000:])
        raise SystemExit(f"tune: selector exited {code}")
    _reasons, cpu = ev.selector_log(err)
    packing, misses, which = _pack_and_misses(ev.load_output(out), answers, durations)
    return packing, misses, which, cpu or 0.0


def _board(sample_ms: int, sample_n: int, hold_ms: int, hold_n: int) -> float:
    sample_rate = sample_ms / sample_n / 1000 if sample_n else 0.0
    hold_rate = hold_ms / hold_n / 1000 if hold_n else 0.0
    return 40 * (0.6 * sample_rate + 0.4 * hold_rate)


def _flags_on(mask: int) -> list[str]:
    return [name for bit, name in enumerate(FLAGS) if mask & (1 << bit)]


def _apply(mask: int) -> None:
    for bit, name in enumerate(FLAGS):
        os.environ[name] = "1" if mask & (1 << bit) else "0"


def _link_holdout(bank: Path, ids: list[str], dest: Path) -> None:
    missing = []
    for commit in ids:
        src = bank / f"{commit}.patch"
        if not src.is_file():
            missing.append(commit)
            continue
        (dest / f"{commit}.patch").symlink_to(src.resolve())
    if missing:
        raise SystemExit(f"tune: holdout patches missing: {' '.join(missing)}")


def main() -> int:
    args = parse_args()
    tests = json.loads(args.tests.read_text())["tests"]
    durations = ev.durations_of(tests)
    sample_dir = args.tests.parent
    sample_answers = json.loads((sample_dir / "answers.json").read_text())["commits"]
    bank_answers = ev.load_bank(args.bank)
    holdout_path = args.holdout or (args.bank / "holdout.txt")
    if not holdout_path.is_file():
        raise SystemExit(f"tune: no holdout list at {holdout_path}")
    hold_ids = [line.strip() for line in holdout_path.read_text().splitlines() if line.strip()]
    missing = [commit for commit in hold_ids if commit not in bank_answers]
    if missing:
        raise SystemExit(f"tune: holdout ids without truth: {' '.join(missing)}")
    hold_answers = {commit: bank_answers[commit] for commit in hold_ids}

    rows = []
    with tempfile.TemporaryDirectory(prefix="tune-holdout-") as tmp:
        hold_dir = Path(tmp)
        _link_holdout(args.bank, hold_ids, hold_dir)
        for mask in range(1 << len(FLAGS)):
            _apply(mask)
            sample_ms, sample_misses, sample_which, sample_cpu = _run(
                args.python, sample_dir / "commits", args.tests, args.sqlglot, args.maps, sample_answers, durations
            )
            hold_ms, hold_misses, hold_which, hold_cpu = _run(
                args.python, hold_dir, args.tests, args.sqlglot, args.maps, hold_answers, durations
            )
            board = _board(sample_ms, len(sample_answers), hold_ms, len(hold_answers))
            rows.append(
                {
                    "mask": mask,
                    "on": _flags_on(mask),
                    "board": board,
                    "sample_s": sample_ms / 1000,
                    "hold_s": hold_ms / 1000,
                    "sample_misses": sample_misses,
                    "hold_misses": hold_misses,
                    "sample_which": sample_which,
                    "hold_which": hold_which,
                    "cpu": sample_cpu + hold_cpu,
                }
            )
            print(
                f"{mask + 1}/{1 << len(FLAGS)} board {board:.3f} "
                f"smiss {sample_misses} hmiss {hold_misses} cpu {sample_cpu + hold_cpu:.3f} "
                f"{'+'.join(_flags_on(mask)) or 'none'}",
                file=sys.stderr,
                flush=True,
            )

    rows.sort(key=lambda row: (row["board"], row["hold_misses"], row["sample_misses"], row["cpu"], row["mask"]))
    print(
        f"{'board':>10} {'sample_s':>10} {'hold_s':>10} {'smiss':>5} {'hmiss':>5} {'cpu':>8}  flags"
    )
    for row in rows:
        print(
            f"{row['board']:10.3f} {row['sample_s']:10.3f} {row['hold_s']:10.3f} "
            f"{row['sample_misses']:5d} {row['hold_misses']:5d} {row['cpu']:8.3f}  "
            f"{'+'.join(row['on']) or 'none'}"
        )

    def best(limit: int | None):
        pool = rows if limit is None else [row for row in rows if row["hold_misses"] <= limit]
        if not pool:
            return None
        # Packing is the board. CPU on an identical selection moves by tenths of a
        # second between runs, so a tie keeps the smaller flag set.
        pool.sort(
            key=lambda row: (
                round(row["board"], 3),
                row["hold_misses"],
                row["sample_misses"],
                round(row["sample_s"], 3),
                round(row["hold_s"], 3),
                len(row["on"]),
                row["cpu"],
                row["mask"],
            )
        )
        return pool[0]

    within = best(1)
    clean = best(0)
    print()
    if within is None:
        print("no combination has ≤ 1 holdout miss")
    else:
        print(
            f"best ≤1 holdout miss: board {within['board']:.3f}  "
            f"sample misses {within['sample_misses']}  holdout misses {within['hold_misses']}  "
            f"cpu {within['cpu']:.3f}  {'+'.join(within['on']) or 'none'}"
        )
        if within["hold_which"]:
            print(f"  holdout misses: {' '.join(within['hold_which'])}")
        if within["sample_which"]:
            print(f"  sample misses: {' '.join(within['sample_which'])}")
    if clean is None:
        print("no combination has 0 holdout misses")
    else:
        print(
            f"best 0 holdout misses: board {clean['board']:.3f}  "
            f"sample misses {clean['sample_misses']}  holdout misses 0  "
            f"cpu {clean['cpu']:.3f}  {'+'.join(clean['on']) or 'none'}"
        )
        if clean["sample_which"]:
            print(f"  sample misses: {' '.join(clean['sample_which'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
