# Blast Radius: plan

Goal: under 500 s on the board. One missed commit = 300 s, so **zero misses comes first**, seconds second.

## What we know (from the 30 samples)

- Oracle (select exactly the failing tests, best packing): **94.4 s**. Full suite, best k per commit: ~1105 s. Starter: 1614 s.
- 12/30 commits break nothing → `[]` costs 0. They're comment-only edits or changes with no behaviour difference (`is`→`==`, `copy=False`→`True`, untested branches).
- Every fixture edit (`tests/fixtures/optimizer/*.sql`) broke exactly one test: the one that loads that file.
- Expensive tests dominate when selected: test_max_nodes 28.8 s, test_execute_tpcds 21.5 s, test_generator_memory 6.7 s. Total suite 103 s.
- Prototype (`prototype/sel.py`, per-test line coverage): **104 s simulated + 5 misses**. All misses were test_optimizer / test_executor tests.
  - Root cause: those tests run work in `ProcessPoolExecutor` (spawn) → coverage doesn't see it. Fix: `tools/conftest_map.py` runs pool jobs inline during the map build. **Not yet re-verified.** Rerun `tools/build_map.sh`, then `make`/score the prototype.

## Layout (target)

```
selector/            runs in the container: stdlib only, loads map once, < 3 s CPU
  main.py            loop /data/commits, print one JSON line each, logs → stderr
  patch.py           unified diff → hunks with OLD line numbers; apply() → new source
  classify.py        file → noop | py_lines | fixture | test_file | unknown
  rules.py           classification → test set (rules below)
  pack.py            try k = 1..8, longest test first onto the least-loaded worker; keep min(max load + 2 s·k)
maps/map.json.gz     committed; built offline (repo limit 20 MB)
tools/               build-time only
  build_map.sh       clone sqlglot@eb11aec, venv from requirements.lock, coverage with test contexts
  conftest_map.py    inline ProcessPool (+ TODO: builtins.open tracing for fixture → tests)
  make_map.py        coverage DB → map (TODO: dedupe sets, JSON+gzip, import_lines, opens, testfile)
  mutate.py          TODO: own mutants, for checking only
  evaluate.py        TODO: selector on sample + mutants → misses per rule, cost
prototype/sel.py     working reference implementation of the rules (hard-coded paths, uses maps/map.pkl)
```

Dockerfile: keep the pinned `python:3.13-slim` digest; ADD the sqlglot source tarball at eb11aec (needed for AST diffs); COPY selector/ and maps/; `chmod -R a+r`. The container runs as `nobody` on a read-only disk.

## Rules (in order, union across files, then pack)

1. `.py` file under `sqlglot/` whose AST is unchanged (`ast.dump` old == new) → nothing.
2. Changed lines: removed lines give old numbers; pure insertions take the lines on both sides. **Map each to the first line of its enclosing statement** (coverage records multi-line statements by their first line).
3. Line covered by tests → those tests.
4. Line runs only at import (empty coverage context: class attributes, dict entries, decorators) → `file_union[file]`. No misses on 4d63, 535z, ktz3 or gbav.
5. Line never executed → nothing.
6. Docstring change → the doctest test if one exists (check).
7. Fixture/data file → tests that open it (trace `open` in the map build; the prototype greps test sources).
8. `tests/*.py` → the whole test file (import error fails every test). helpers/conftest → everything.
9. Anything else → full suite until mutants show that's too cautious.

## Checking (zero misses)

- `mutate.py`: 300+ one-line mutants: `and`↔`or`, `==`↔`!=`, `<`↔`<=`, `True`↔`False`, `is`→`==`, numbers/strings (incl. class-level dicts), comment insertions, fixture character corruption, multi-file combos.
- Ground truth: run the real suite the way the organizers labeled (requirements.lock, `SKIP_INTEGRATION=0`, real pools, xdist). Run it overnight.
- Mutants are for **checking only**. The rules ban a table from a patch or kind of edit to the tests it breaks.

## Schedule

| Time | Raghavan | Teammate |
|---|---|---|
| 18:30–19:45 | build_map.sh → verify unmapped contexts = 1, finish make_map.py | patch.py, classify.py, pack.py, main.py on sample/ |
| 19:45–20:15 | rules.py, `make sample`: 0 misses, ~150–250 s | Dockerfile; check it runs as `nobody` on a read-only disk |
| ~20:15 | **Submit v1** (1 submission/hour) | |
| 20:30–23:00 | mutate.py + evaluate.py, start the overnight run | narrow import-time lines to tests reading that attribute in that dialect |

Saturday: fix every mutant miss, then tighten rules. Cutoff: Sun 11:00 AM ET.
