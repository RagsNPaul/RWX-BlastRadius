# Bus plan (2 hours): from 295.7 s toward the leader

## Where we stand
- Board: **295.7 s** (V3-1, import check) = **7.4 s/commit** over ~40 commits, 0 misses.
- Leader: ≈ 143–160 s = **3.6–4.0 s/commit**. Best possible ≈ 3.2 s/commit (samples), 2.0 (test patches).
- To win we must remove **≈ 3.5 s per commit**.

## Why we're slow (measured today, `733e1b7` + uncommitted V3-1)

| Cost bucket, s/commit | Samples | Test patches | Cause |
|---|---|---|---|
| best possible | 3.15 | 2.00 | unavoidable |
| **slow tests that pass** | 1.39 | 2.92 | coverage pulls in test_execute_tpcds (21.5 s), test_optimized_tpch, test_execute_tpch, test_generator_memory, test_max_nodes. **One slow test sets the whole commit's cost.** |
| **other over-selection** | 1.28 | **3.78** | line + class + word rules each add tests "just in case". Most of those tests run the changed line but don't check its effect. |
| "all tests" fallbacks | 0 | 1.06 | import-time edits the import check can't rule out |

**Coverage tells us which tests *could* fail, not which *will*.** We're at the limit of being purely safe, so the leader must be taking calculated risks or checking cheaply. That's what both plans below do.

## Ground rules for the ride
- Submissions are 2+/hour: **use the board as an experiment**. It reports misses and simulated seconds for each run.
- One change per submission, so each score tells us exactly what that change did.
- Write every submission in RESULTS.md: git hash, flags, sample s, holdout misses, board s, board misses.
- **The final entry is chosen deliberately at the end, not by default** (see the last section).

---

## HOUR 1: Plan A, finish the current approach (2–3 submissions)

### A0 (5 min): commit and push what exists
> Commit everything except bank/logs and _snap/: selector/fingerprint.py, selector/probe.py, rules.py, maps/import_fp.json, maps/tpc.json, maps/max_nodes_lines.json, tools/*. Push to main. Check `make sample` still gives 174.4 s / 0 misses in Docker, or locally if Docker isn't available on the bus.

### A1 (~25 min) → SUBMIT #1: slow-test checks (V3-2)
> Finish V3-2 from V3.md, behind EXPENSIVE_CHECK=1: TPC-H/TPC-DS executor tests are dropped when optimizing their fixture queries gives identical SQL under the patch and the patch touches nothing in sqlglot/executor/. Same patched copy as the import check. test_max_nodes only for changes in parser.py, tokens.py, tokenizer_core.py, expressions/core.py, or lines listed in maps/max_nodes_lines.json. test_generator_memory only for changes in generator.py or expressions/core.py. Print the check CPU per commit. Report sample s, holdout s and misses with it on and off. Ship if holdout misses ≤ 1 and s/commit drops.
- Targets the "slow tests that pass" bucket: −1.4 s/commit (samples) to −2.9 (test patches).
- **Expected board: ~230–260 s.**

### A2 (~20 min) → SUBMIT #2: cut the "other over-selection"
> Add a per-rule cost report: for every commit, how many seconds each rule (line, class, word, neighbor lines, dialect test file) added beyond the true failing set. Then turn on, one flag at a time, whichever has the biggest waste and ≤ 1 holdout miss: WORD_NO_EXPENSIVE (word rule may not add tests over 1 s), WORD_OFF (drop the word rule except for string literals that were removed), NEIGHBOR_OFF, DIALECT_FILE_OFF, CLASS_CAP (class-usage over 400 tests → file_union). Report net = saved − 300 × extra holdout misses for each.
- **Expected board: ~200–240 s.**

---

## HOUR 2: Plan B, a different approach: risk-priced selection (3–4 submissions)

**Idea:** stop treating every covered test as equally likely to fail. Score each candidate test's chance of failing with a small model on general features, then keep a test only if `P(fail) × 300 s` is more than what it costs to run. This is how Meta's predictive test selection works (catches >99.9% of bad changes while running far fewer tests). We tune the risk level on the board itself.

**Rules compliance:** features must be general (no test names or ids, no patch text as a lookup). Otherwise it's a "table from a kind of edit to tests", which is banned, and top entries get read.

### B1 (~30 min): build the model
> Add tools/eval/risk_model.py. For every (commit, candidate test) in sample + bank (non-holdout), where candidates = the tests the current safe selector picks, build features: (1) number of changed lines this test covers, (2) number of tests covering those lines (log), (3) the test's total covered-line count (log: very broad tests rarely fail for one line), (4) does the test file's name match the changed file's dialect/module (duckdb.py ↔ test_duckdb.py, optimizer/x.py ↔ test_optimizer.py), (5) change kind: line / class-level / import-time / fixture, (6) test duration (log), (7) is the changed line inside an if/elif condition vs a plain statement. Label = test failed. Fit logistic regression with plain-Python gradient descent (stdlib only, no numpy). Save weights to maps/risk.json (8 numbers). Report holdout AUC and calibration in 5 bins.
> In rules.py behind RISK=<threshold>: after the safe selection, sort candidate tests by `P(fail)` ascending. Drop tests while the commit's summed P(fail) of dropped tests stays below the threshold (e.g. 0.03). Never drop tests from fixture rules, and never empty a selection the safe selector made non-empty unless the summed P is below threshold/3. Report sample s, holdout s, holdout misses for thresholds 0.01, 0.03, 0.05, 0.10.

### B2 → SUBMIT #3, #4, #5: tune the risk level on the board
- **#3: threshold 0.02** (conservative). Expect −1 to −2 s/commit, 0–1 board misses.
- **#4: threshold 0.05.** Expect bigger savings, maybe 1 board miss.
- **#5: threshold 0.10** only if #4 showed 0 misses.
- Each board run reports `simulated` and `misses`. **Score without misses = simulated − 300 × misses**: that's the pure efficiency, so you can see the curve even when a run misses.

### B3: combine
> If Plan B's best threshold beats Plan A's submission on the board, combine: Plan A's checks first (they're exact), then the risk model on what's left. Submit once more if time allows.

---

## Choosing the final entry (last 10 minutes, and again before Sun 11:00)
The final has ~60 unseen commits (~1.5× the board) with new kinds of change. Expected final ≈ 1.5 × (board simulated − 300 × board misses) + 300 × expected final misses.

| Candidate | Use when |
|---|---|
| Lowest board score with **0 board misses and ≤ 1 holdout miss** | default choice |
| A risk-priced version with 1 board miss | only if its efficiency (simulated − 300 × misses) is ≥ 40 s better than the safe one; that's roughly break-even |
| **Never** `6b5c4ae` (288 s) | it misses 4 of our 134 test patches |

## If something breaks on the bus
- Board rejects a build: resubmit the previous hash and keep going. It never lowers your best score.
- No Docker: run `python3 -S selector/main.py --data sample --src sqlglot --maps maps` plus `score.py`, which is enough for sample s and misses. Holdout via `tools/eval/ev.py`.
- Out of battery or time: the last submitted safe version stands. Pick the final entry from the board later.
