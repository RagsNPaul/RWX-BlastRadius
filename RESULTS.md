# Results

| date | git | sample s | sample misses | bank | bank misses | bank s | cpu s | board s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-20 | 542ba89 dirty | 184.623 | 0 | 20 | 0 | 182.261 | 2.212 | |
| 2026-11-20 | 6b5c4ae dirty | 185.052 | 0 | 47 | 0 | 640.493 | 0.369 + 6.657 | |
| 2026-11-20 | 6b5c4ae dirty | 174.411 | 0 | 54 | 0 | 685.719 | 0.546 + 8.668 | |
| 2026-11-20 | 6b5c4ae dirty | 174.411 | 0 | 59 | 0 | 789.975 | 0.510 + 8.479 | |
| 2026-11-21 | 6b5c4ae dirty | 174.411 | 0 | 89 | 2 | 1559.041 | 0.370 + 7.059 | |
| 2026-11-21 | 6b5c4ae dirty | 174.411 | 0 | 93 | 2 | 1559.041 | 0.473 + 7.979 | |
| 2026-11-21 | 6b5c4ae dirty | 174.411 | 0 | 98 | 2 | 1689.232 | 0.330 + 6.091 | |
| 2026-11-21 | 6b5c4ae dirty | 174.411 | 0 | 102 | 2 | 1744.775 | 0.324 + 6.007 | |
| 2026-11-21 | 733e1b7 dirty | 174.411 | 0 | 134 | 0 | 1556.952 | 3.780 bank | |
| 2026-11-21 | 733e1b7 dirty | 174.411 | 0 | 134 | 0 | 1307.884 | 0.232 + 13.979 | 295.694 |

IMPORT_CHECK default on. Flag off is the 1556.952 row (0 misses after the corrected truths). Flag on saves 249.068 s on the bank and 104.696 s on the holdout, 0 extra misses. r7_11 stays all 1620 tests (`all:import-class`, import failed). Check CPU mean 0.508 s on the bank (20 checks, max 0.548 s). Holdout packing 521.013 → 416.317.

V3-3 flags stay off. Measured alone on sample (174.411 s, 0 misses) + holdout (416.317 s, 0 misses). net = packing saved − 300 × extra misses − extra CPU. None reduced packing on that set. WORD_NO_EXPENSIVE net −0.244. CLASS_CAP net +0.047 (CPU noise; the one holdout change, r8_classattr_11, selected 1583 → 1585 tests). NEIGHBOR_OFF net −0.113. DIALECT_FILE_OFF net +0.104 (CPU noise, no selection change). On the rest of the bank, WORD_NO_EXPENSIVE saves 18.483 s on m0007 with 0 misses, and CLASS_CAP misses test 201 on r8_classattr_14.

V3-4 `tools/eval/tune.py`, 64 combinations, sample + holdout, 0 misses in every row. Board = 40 × (0.6 × sample s/commit + 0.4 × holdout s/commit). IMPORT_CHECK on: sample 174.411, holdout 416.317, board 306.056, CPU 10.661–13.639. IMPORT_CHECK off: sample 174.411, holdout 521.013, board 347.934, CPU 3.477–5.387. EXPENSIVE_CHECK, WORD_NO_EXPENSIVE, CLASS_CAP, NEIGHBOR_OFF, and DIALECT_FILE_OFF do not change packing on this set (EXPENSIVE_CHECK is not applied in `select`). Same board, so the smaller flag set wins: IMPORT_CHECK on, the other five off. That was already the default. The ≤1-miss pick and the 0-miss pick are the same.

EXPENSIVE_CHECK stays off. IMPORT_CHECK on for both rows. Off: sample 174.411 s (5.814 s/commit, 0 misses, CPU 0.528), holdout 416.317 s (10.408 s/commit, 0 misses, CPU 10.067). On: sample 154.441 s (5.148 s/commit, 1 miss `3utz` drops failing `test_execute_tpcds` and `test_execute_tpch` on `sqlglot/serde.py`, CPU 10.107), holdout 397.819 s (9.945 s/commit, 0 misses, CPU 44.639). Holdout packing falls by 18.498 s and the check CPU rises by 34.572 s, so the CPU-inclusive net is −16.074 s. r7_11 stays all 1620 tests.

Per-rule packing seconds beyond the failing set (`tools/eval/rule_cost.py`). Sample: line 57.630, class 22.408, word 0, neighbor 0, dialect 0. Holdout: line 95.507, class 112.287, word 0, neighbor 0, dialect 0. Bank: line 254.133, class 288.139, word 29.011 (20.770 of it from tests over 1 s), neighbor 0, dialect 0. Each flag alone, net = holdout packing saved − 300 × extra holdout misses, 0 misses on sample and holdout: CLASS_CAP 0, DIALECT_FILE_OFF 0, NEIGHBOR_OFF 0, WORD_NO_EXPENSIVE 0, WORD_OFF 0. The class waste is real and CLASS_CAP does not remove it. All five stay off. WORD_OFF after the net-removed-literal fix changes 0 holdout selections and 0 bank commits.

`RISK` stays unset. `maps/risk.json` is eight weights from logistic regression on 19763 safe-selector candidates (1450 failures) in the sample plus the non-holdout bank. Holdout: 6788 rows, 67 failures, AUC 0.468. Calibration, equal-width bins: 0.0–0.2 n=6609 mean P 0.074 fail 0.010; 0.2–0.4 n=155 mean P 0.308 fail 0.000; 0.4–0.6 n=24 mean P 0.460 fail 0.125; 0.6–0.8 and 0.8–1.0 empty. Packing seconds, miss penalty not included:

| RISK | sample s | sample misses | holdout s | holdout misses |
| --- | ---: | ---: | ---: | ---: |
| 0.01 | 174.398 | 2 (3utz, ary6) | 416.312 | 0 |
| 0.03 | 173.457 | 5 | 415.503 | 6 |
| 0.05 | 171.079 | 6 | 414.699 | 6 |
| 0.10 | 170.359 | 6 | 408.136 | 9 |

`SLOW_POLICY` default on (`b8bd171`), last step of `select`. Drops `test_max_nodes` and `test_generator_memory` always, and the three TPC executor tests unless a changed path starts with `sqlglot/optimizer/`, `sqlglot/executor/`, or `sqlglot/planner`, is `sqlglot/serde.py`, or contains `tpc`. Skipped when the commit reason contains `all:`. `ev.py`: sample 154.288 s, 0 misses, CPU 0.474 s. Bank 1100.199 s, 0 misses, CPU 28.277 s. r7_11 stays all 1620 tests (`all:import-class`) and still fails `test_execute_tpcds`, `test_execute_tpch`, `test_optimized_tpch`, and `test_generator_memory`. `3utz` (`sqlglot/serde.py`) keeps the two failing TPC tests.
