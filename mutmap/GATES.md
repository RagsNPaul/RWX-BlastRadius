# GATES

## Gate P2

Warm worker RSS 170655744 bytes (0.159 GiB). Available memory 3.360 GiB (free + inactive + speculative). `workers = min(9, floor(3.360 / (0.159 × 1.5))) = 9`.

| test | required | measured | PASS/FAIL |
| --- | --- | --- | --- |
| 1. Warm-up | ≤ 60 s | 1.958 s | PASS |
| 2. Workers from RSS | ≥ 4 | 9 | PASS |
| 3. No-op children | 0 failures on 20 random lines, tests ≤ 3 s | 0 failures | PASS |
| 4. Child overhead | mean ≤ 0.10 s over 20 runs | 0.0090 s | PASS |
