#!/usr/bin/env python3
"""Fit P(fail) for the safe selector's candidates and score RISK thresholds.

  python3 tools/eval/risk_model.py --sqlglot sqlglot --python .venv/bin/python \
      --tests sample/tests.json --bank bank --maps maps

Training rows are every (commit, candidate test) in the sample and the bank
outside the holdout. The candidate set is what the safe selector picks.
maps/risk.json is eight weights: intercept, changed lines covered, log of how
many tests cover those lines, log of the test's covered-line count, name match,
change kind (0 line, 1 class, 2 import-time, 3 fixture), log duration, and
whether a covered changed line is an if/elif condition.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools" / "eval"))
sys.path.insert(0, str(ROOT / "selector"))

import ev  # noqa: E402
from pack import pack  # noqa: E402
from patch import parse  # noqa: E402
from rules import Maps, fail_probability, risk_features, select  # noqa: E402

THRESHOLDS = (0.01, 0.03, 0.05, 0.10)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlglot", required=True, type=Path)
    parser.add_argument("--python", required=True)
    parser.add_argument("--tests", required=True, type=Path)
    parser.add_argument("--bank", required=True, type=Path)
    parser.add_argument("--maps", required=True, type=Path)
    parser.add_argument("--holdout", type=Path)
    return parser.parse_args()


def _fit(rows: list[tuple], labels: list[int], epochs: int = 250, rate: float = 0.3, penalty: float = 1e-4):
    """Logistic regression. Returns weights for the raw features, scale folded in."""
    count = len(rows)
    width = len(rows[0])
    means = [0.0] * width
    for row in rows:
        for index, value in enumerate(row):
            means[index] += value
    means = [value / count for value in means]
    variances = [0.0] * width
    for row in rows:
        for index, value in enumerate(row):
            delta = value - means[index]
            variances[index] += delta * delta
    scales = [math.sqrt(value / count) or 1.0 for value in variances]
    scaled = [
        tuple((row[index] - means[index]) / scales[index] for index in range(width))
        for row in rows
    ]
    weights = [0.0] * (width + 1)
    order = list(range(count))
    rng = random.Random(0)
    loss = 0.0
    for epoch in range(epochs):
        rng.shuffle(order)
        grad = [0.0] * (width + 1)
        loss = 0.0
        for index in order:
            score = weights[0]
            features = scaled[index]
            for weight, value in zip(weights[1:], features):
                score += weight * value
            probability = fail_probability([score, 0.0], (0.0,))
            error = probability - labels[index]
            clipped = min(max(probability, 1e-12), 1 - 1e-12)
            loss -= labels[index] * math.log(clipped) + (1 - labels[index]) * math.log(1 - clipped)
            grad[0] += error
            for feature_index, value in enumerate(features):
                grad[feature_index + 1] += error * value
        for index in range(width + 1):
            regular = penalty * weights[index] if index else 0.0
            weights[index] -= rate * (grad[index] / count + regular)
        if epoch == 0 or epoch + 1 == epochs or (epoch + 1) % 50 == 0:
            print(f"epoch {epoch + 1} loss {loss / count:.4f}", file=sys.stderr, flush=True)
    raw = [weights[0]]
    for index in range(width):
        raw.append(weights[index + 1] / scales[index])
        raw[0] -= weights[index + 1] * means[index] / scales[index]
    return raw


def _auc(scores: list[float], labels: list[int]) -> float:
    positives = sum(labels)
    negatives = len(labels) - positives
    if not positives or not negatives:
        return float("nan")
    order = sorted(range(len(scores)), key=lambda index: scores[index])
    ranks = [0.0] * len(scores)
    start = 0
    while start < len(order):
        end = start
        while end + 1 < len(order) and scores[order[end + 1]] == scores[order[start]]:
            end += 1
        average = (start + 1 + end + 1) / 2
        for index in range(start, end + 1):
            ranks[order[index]] = average
        start = end + 1
    rank_sum = sum(ranks[index] for index, label in enumerate(labels) if label)
    return (rank_sum - positives * (positives + 1) / 2) / (positives * negatives)


def _bins(scores: list[float], labels: list[int], bins: int = 5):
    rows = []
    for index in range(bins):
        low = index / bins
        high = (index + 1) / bins
        chosen = [
            (score, label)
            for score, label in zip(scores, labels)
            if (score >= low and score < high) or (index == bins - 1 and score >= low)
        ]
        if not chosen:
            rows.append((low, high, 0, 0.0, 0.0))
            continue
        mean = sum(score for score, _label in chosen) / len(chosen)
        rate = sum(label for _score, label in chosen) / len(chosen)
        rows.append((low, high, len(chosen), mean, rate))
    return rows


def _collect(patch: Path, maps: Maps, failing: set):
    blame: list = []
    files = parse(patch.read_text(encoding="utf-8"))
    selected, _reasons = select(files, maps, blame)
    features = risk_features(files, maps, blame, set(selected))
    rows, labels = [], []
    for test, values in features.items():
        rows.append(values)
        labels.append(1 if test in failing else 0)
    return rows, labels


def _score(commits: list[tuple[str, Path]], answers: dict, maps: Maps, durations: list[int]):
    total = 0
    missed = []
    for commit, patch in commits:
        files = parse(patch.read_text(encoding="utf-8"))
        selected, _reasons = select(files, maps)
        chosen = set(selected)
        total += ev.packing_cost(pack(sorted(chosen), durations), durations)
        if set(answers[commit]["failing"]) - chosen:
            missed.append(commit)
    return total / 1000, missed


def main() -> int:
    args = parse_args()
    os.environ.pop("RISK", None)
    tests = json.loads(args.tests.read_text())["tests"]
    durations = ev.durations_of(tests)
    maps = Maps(str(args.maps), str(args.sqlglot))
    maps.check(tests)
    sample_dir = args.tests.parent
    sample_answers = json.loads((sample_dir / "answers.json").read_text())["commits"]
    bank_answers = ev.load_bank(args.bank)
    hold_path = args.holdout or (args.bank / "holdout.txt")
    hold_ids = {line.strip() for line in hold_path.read_text().splitlines() if line.strip()}
    train_rows, train_labels = [], []
    hold_rows, hold_labels = [], []

    def add(patch: Path, failing: set, hold: bool):
        rows, labels = _collect(patch, maps, failing)
        if hold:
            hold_rows.extend(rows)
            hold_labels.extend(labels)
        else:
            train_rows.extend(rows)
            train_labels.extend(labels)

    for commit, answer in sorted(sample_answers.items()):
        add(sample_dir / "commits" / f"{commit}.patch", set(answer["failing"]), False)
        print(f"sample {commit} train {len(train_rows)}", file=sys.stderr, flush=True)
    seen = 0
    for commit, answer in sorted(bank_answers.items()):
        if commit in hold_ids:
            continue
        add(args.bank / f"{commit}.patch", set(answer["failing"]), False)
        seen += 1
        if seen % 20 == 0:
            print(f"bank {seen} train {len(train_rows)}", file=sys.stderr, flush=True)
    print(f"train rows {len(train_rows)} positives {sum(train_labels)}", file=sys.stderr, flush=True)
    for commit in sorted(hold_ids):
        add(args.bank / f"{commit}.patch", set(bank_answers[commit]["failing"]), True)
    print(f"holdout rows {len(hold_rows)} positives {sum(hold_labels)}", file=sys.stderr, flush=True)

    weights = _fit(train_rows, train_labels)
    out = args.maps / "risk.json"
    out.write_text(json.dumps([round(value, 6) for value in weights]) + "\n", encoding="utf-8")
    os.chmod(out, 0o644)
    maps._risk = None
    scores = [fail_probability(weights, row) for row in hold_rows]
    print(f"weights {', '.join(f'{value:.4f}' for value in weights)}")
    print(f"holdout AUC {_auc(scores, hold_labels):.3f}  rows {len(hold_rows)}  positives {sum(hold_labels)}")
    print(f"{'bin':<14} {'n':>8} {'mean_p':>8} {'fail':>8}")
    for low, high, count, mean, rate in _bins(scores, hold_labels):
        print(f"{low:.1f}-{high:.1f}{'':<6} {count:8d} {mean:8.3f} {rate:8.3f}")

    sample_commits = [(commit, sample_dir / "commits" / f"{commit}.patch") for commit in sorted(sample_answers)]
    hold_commits = [(commit, args.bank / f"{commit}.patch") for commit in sorted(hold_ids)]
    print(f"{'threshold':>10} {'sample_s':>10} {'hold_s':>10} {'hmiss':>6}")
    for threshold in THRESHOLDS:
        os.environ["RISK"] = f"{threshold}"
        sample_s, sample_miss = _score(sample_commits, sample_answers, maps, durations)
        hold_s, hold_miss = _score(hold_commits, bank_answers, maps, durations)
        print(f"{threshold:10.2f} {sample_s:10.3f} {hold_s:10.3f} {len(hold_miss):6d}")
        if sample_miss:
            print(f"  sample misses: {' '.join(sample_miss)}")
        if hold_miss:
            print(f"  holdout misses: {' '.join(hold_miss)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
