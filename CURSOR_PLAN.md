# Sub-200 s plan: loop and Cursor prompts

Rules live in `.cursor/rules/` (00 always on; 10/20/30 attach by folder; 40 = submit checklist).
Each prompt below is one BUILD step. After each one, run the TEST step, then the GATE.

## Score budget (30 samples, zero misses)

| Piece | Estimate | Lever |
|---|---|---|
| Commits that must run tests (18) | ~120–160 s | oracle for these is 94 s; extra = over-selection |
| Commits that break nothing (12) | 0–15 s | `[]` via AST-equal and uncovered lines; import-time lines cost ~6–8 s each until narrowed |
| CPU | 0.5–1.5 s | stdlib, lazy per-file map |
| **Total** | **~125–175 s** | the 197.2 s leader is in this range: zero misses + coverage selection |

One miss adds 300 s and puts you behind the leader. Zero misses is the whole game.

## Iteration plan (each ≈ 45–60 min = one submission slot)

| # | BUILD | Expected sample s | Gate to submit |
|---|---|---|---|
| 0 | Map rebuild with the inline pool (Prompt 1) | prototype: 0 misses | — (no submit) |
| 1 | Selector v1: rules 1–9 + LPT packing + Dockerfile (Prompts 2–3) | ~150–180 | 0 misses on sample |
| 2 | Eval harness + first 20 mutants (Prompts 4–5) | same | 0 misses on bank |
| 3 | Fixture tracing (`opens`) + statement mapping hardening | −0 to −5 | bank grows +20 |
| 4 | CPU: lazy per-file map, compact output | −0.5 to −1 CPU | |
| 5 | Narrow import-time lines (attribute refs ∩ dialect users) | −10 to −15 | 0 misses incl. 20 new class-attribute mutants |
| 6+ | Exact packing for small sets; per-rule audits from the bank | −1 to −5 | |

Run ground truth for mutants in the background during every BUILD.

## Prompts (paste into Cursor in order)

**Prompt 1: map build**
> Follow .cursor/rules/20-map-build.mdc. Run tools/build_map.sh as-is first and confirm exactly 1 unmapped context. Then rewrite tools/make_map.py to emit maps/index.json, maps/sets.json and maps/lines/<path>.json as specified (deduplicated test sets, import_lines from empty contexts, testfile from tests.json). Extend tools/conftest_map.py to record builtins.open / Path.open / Path.read_text of non-.py files per test (use the PYTEST_CURRENT_TEST env var to know the current test) into maps/opens.json. Print total map size. Then rerun prototype/sel.py on sample/ with the old pickle path adapted and report misses.

**Prompt 2: selector v1**
> Follow .cursor/rules/10-selector.mdc exactly. Create selector/patch.py (unified-diff parser with old line numbers + apply), classify.py, rules.py (rules 1–9 in order), pack.py (k=1..8 LPT, exact search ≤12 tests), main.py (reads /data/commits and /data/tests.json, per-commit try/except → full suite on error, rusage CPU to stderr). Port the logic from prototype/sel.py; don't import it. Original sqlglot sources are read from /opt/sqlglot. Make `python3 selector/main.py --data sample` work locally too.

**Prompt 3: Dockerfile**
> Update the Dockerfile: keep the pinned base digest; ADD https://github.com/tobymao/sqlglot/archive/eb11aec4be8cb2122afe713a4816662c3f0fb2fa.tar.gz, extract only sqlglot/ and tests/ to /opt/sqlglot; COPY selector/ and maps/ to /app; chmod -R a+rX /app /opt/sqlglot; CMD python -S /app/selector/main.py. Then run make sample and make check and report simulated seconds, misses and the CPU line from stderr.

**Prompt 4: ground truth**
> Follow .cursor/rules/30-eval-loop.mdc. Write tools/groundtruth.sh: for each bank/<id>.patch without bank/<id>.json, copy a pristine sqlglot checkout to a temp dir, git apply the patch, run pytest inside the pinned python:3.13-slim image with requirements.lock and SKIP_INTEGRATION=0 using -n auto -rA -p no:cacheprovider, parse failures (FAILED, ERROR, SUBFAILED, collection errors → all tests of that file) to tests.json numbers, write bank/<id>.json. Validate by running it on 3 sample patches and comparing to sample/answers.json exactly.

**Prompt 5: mutants + evaluate**
> Write tools/mutate.py (N mutants, kinds and weights from 30-eval-loop.mdc, seeded, outputs valid unified diffs against the pinned sources, ids never reused) and tools/evaluate.py (runs selector over sample/ and bank/, uses score.py arithmetic, prints misses with ids, simulated s, per-rule counts, CPU s; exits non-zero on any miss; appends to RESULTS.md). Generate 20 mutants, run groundtruth.sh, then evaluate.py.

**Prompt 6: tighten one rule (repeat per rule)**
> Rule under change: <name>. Show me the bank mutants and samples it affects. Implement the narrower version behind a flag, generate 15 NEW mutants targeting it, get ground truth, run evaluate.py with flag on and off. Only keep the flag on if misses == 0 in both and the total improves. Log both runs in RESULTS.md.

## What the research says (and how it shaped the rules)
- Ekstazi-style RTS: track dynamic dependencies (files read) per test; "safe" = selects every affected test. → coverage + `opens` map, select-more-when-unsure.
- RTS across process boundaries: dependencies created in spawned processes are missed unless traced. → the inline-pool conftest (our 5 misses).
- pytest-testmon: unexecuted code can't change a test's outcome; executed blocks are checksummed. → rules 3 and 5.
- Trivial Compiler Equivalence: compare compiled forms to spot equivalent mutants; catches about 30% of them. → rule 1 (AST equality).
- Meta predictive test selection: ML probability of failure, catches about 99.9% of faulty changes. Not usable here: no history, and a patch→tests table is banned. 300 s per miss also favours safe RTS.
- LPT scheduling: within 4/3 of the optimal makespan; exact search for small sets closes the gap.
