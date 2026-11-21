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
