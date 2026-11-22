# GATES

## Gate P2

Warm worker RSS 170655744 bytes (0.159 GiB). Available memory 3.360 GiB (free + inactive + speculative). `workers = min(9, floor(3.360 / (0.159 × 1.5))) = 9`.

| test | required | measured | PASS/FAIL |
| --- | --- | --- | --- |
| 1. Warm-up | ≤ 60 s | 1.958 s | PASS |
| 2. Workers from RSS | ≥ 4 | 9 | PASS |
| 3. No-op children | 0 failures on 20 random lines, tests ≤ 3 s | 0 failures | PASS |
| 4. Child overhead | mean ≤ 0.10 s over 20 runs | 0.0090 s | PASS |

## Gate P4

Dry run of pass 1, `--workers 5 --limit 300`. A warm worker plus its forked child was 303 MB resident. Nine of those would put total RSS over 90% of the 3.9 GiB available, so N = 5. Killed after 152 results were on disk (153 flushed), then restarted the same command.

The 300 lines are the cheapest priority-folder lines: mean covering-test cost 0.0015 s, versus 10.623 s for all 36,200 covered lines. Raw lines/hour is the cheap-line rate.

| test | required | measured | PASS/FAIL |
| --- | --- | --- | --- |
| 1. Restart | 0 mutants redone; all 300 complete | already 153, queued 147, overlap 0, final 300 unique, file prefix identical | PASS |
| 2. Lines/hour | ≥ 3257.1 = 0.6 × 10857 × 5 / 10 | raw 84781 (killed run, includes warmup); restart raw 92245 | PASS |
| 3. Worker RSS | ≤ 90% of available for the whole run | max 0.309 (1,226,489,856 bytes) on the killed run; 0.230 on the restart | PASS |
| 4. error status | ≤ 2% | 4/300 = 1.333% (0 timeouts) | PASS |

## Gate P5

Overnight launch at 22:39:40, checked at 22:49:58. Pass 1, `--workers 5 --until 03:30`, still running under `caffeinate`. Pass 2 is chained for 08:00 and has not started. Jobs are streamed so workers start before the full queue is built.

Progress line: `progress done=5800 lines_per_hour=34251.2 eta_s=16810.8 rss_bytes=401899520 rss_frac=0.099`

| test | required | measured | PASS/FAIL |
| --- | --- | --- | --- |
| 1. Lines/hour | ≥ 3257.1 = 0.6 × 10857 × 5 / 10 | 34251.2 | PASS |
| 2. Crashes and errors | 0 worker crashes; error rate ≤ 2% | 0 crashes (parent, caffeinate, and 5 workers alive; no traceback); 110/5866 = 1.875% errors (6 timeouts) | PASS |
| 3. Memory | ≤ 90% of available for the whole 10 minutes | max rss_frac 0.842 (2,894,954,496 bytes) at done=2000 | PASS |
