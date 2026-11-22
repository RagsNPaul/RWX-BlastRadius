#!/usr/bin/env python3
"""Build maps/sens from mutmap/results.jsonl.

A line is written only when it has at least one ok or timeout mutant. The set is
the union of failing tests over those mutants. clean is 1 only when the line
has at least one ok mutant and every ok mutant failed nothing. error and
import_time rows are not a measurement, so those lines keep the coverage rule.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MEASURED = ("ok", "timeout")


def covered_lines(lines_dir: Path) -> int:
    total = 0
    for path in lines_dir.rglob("*.json"):
        total += len(json.loads(path.read_text(encoding="utf-8")))
    return total


def load_rows(path: Path) -> dict[tuple[str, int], dict]:
    grouped: dict[tuple[str, int], dict] = {}
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            row = json.loads(raw)
            if row.get("status") not in MEASURED:
                continue
            key = (row["file"], int(row["line"]))
            bucket = grouped.setdefault(key, {"ok": [], "timeout": []})
            failing = tuple(sorted(set(row.get("failing") or ())))
            bucket[row["status"]].append(failing)
    return grouped


def build(results: Path, maps: Path) -> tuple[int, int, int]:
    grouped = load_rows(results)
    sets: dict[tuple[int, ...], int] = {}
    ordered: list[list[int]] = []
    by_file: dict[str, dict[str, list[int]]] = {}
    for (rel, line), bucket in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1])):
        union: set[int] = set()
        for failing in bucket["ok"]:
            union.update(failing)
        for failing in bucket["timeout"]:
            union.update(failing)
        clean = 1 if bucket["ok"] and all(len(failing) == 0 for failing in bucket["ok"]) else 0
        key = tuple(sorted(union))
        index = sets.get(key)
        if index is None:
            index = len(ordered)
            sets[key] = index
            ordered.append(list(key))
        by_file.setdefault(rel, {})[str(line)] = [index, clean]

    sens_root = maps / "sens"
    sens_root.mkdir(parents=True, exist_ok=True)
    written_lines = 0
    for rel, table in by_file.items():
        dest = sens_root / (rel + ".json")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(table, separators=(",", ":")) + "\n", encoding="utf-8")
        os.chmod(dest, 0o644)
        written_lines += len(table)
    sets_path = maps / "sens_sets.json"
    sets_path.write_text(json.dumps(ordered, separators=(",", ":")) + "\n", encoding="utf-8")
    os.chmod(sets_path, 0o644)
    return written_lines, len(ordered), covered_lines(maps / "lines")


def main() -> int:
    results = ROOT / "mutmap" / "results.jsonl"
    maps = ROOT / "maps"
    if len(sys.argv) > 1:
        results = Path(sys.argv[1])
    lines, n_sets, covered = build(results, maps)
    percent = 100.0 * lines / covered if covered else 0.0
    print(f"sens_lines {lines}")
    print(f"sens_sets {n_sets}")
    print(f"covered_lines {covered}")
    print(f"coverage {lines}/{covered} = {percent:.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
