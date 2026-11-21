# Evaluation tools

Paths are flags. `make eval` runs ground truth, then the scorer.

- `gt.py --sqlglot DIR --python PY --tests sample/tests.json --bank bank [patch ...]`
  Copies `--sqlglot`, applies each patch with `git apply`, and runs pytest with
  `-n auto`, `SKIP_INTEGRATION=0`, `--timeout=120`, `-p no:cacheprovider`, `-p gtplug`.
  Writes `bank/<id>.json` next to the patch and skips a patch that already has one.
  `gtplug.py` records FAILED, ERROR, SUBFAILED, and collection errors (every test in that file).
  The check that this matches the labeled sample is the four commits rszg, 2jrq, 66ks, 986c,
  run with the pinned image's Python (its SQLite, not the host's).
- `mutate.py --sqlglot DIR --maps maps --kind KIND SEED N OUTDIR`
  KIND is `random` (lines in `maps/lines` and `import_lines`), `classattr`, `enumcmp`,
  `copyflag`, `deadbranch`, `fixture`, or `comment`. Ids are `r<seed>_<kind>_<nn>`
  and an existing patch is never overwritten.
- `buckets.py --sqlglot DIR --python PY --tests sample/tests.json --bank bank --maps maps`
  Runs the selector on the sample and the bank. Prints, for each set and for the
  frozen bank holdout (`bank/holdout.txt`, 30% of bank ids, seed 0, kept once written),
  the cost split into best, fallback (`all:` reasons), expensive-test waste, and other,
  plus a blended board estimate. Later reports keep the holdout separate.
  Truths that list `test_max_nodes` or `test_execute_tpcds` are remeasured in the
  pinned image with `gt.py --timeout=600 -n 4`.
- `tune.py --sqlglot DIR --python PY --tests sample/tests.json --bank bank --maps maps`
  Runs all 64 on/off mixes of `IMPORT_CHECK`, `EXPENSIVE_CHECK`, and the four V3-3 flags
  on the sample and on `bank/holdout.txt`. Prints them sorted by the V3-0 board estimate
  (holdout s/commit in place of the bank), with sample misses, holdout misses, and CPU.
- `ev.py --sqlglot DIR --python PY --tests sample/tests.json --bank bank --maps maps`
  Reads `sample/answers.json`, every `bank/<id>.json`, and `bank/truth.json`.
  Runs `selector/main.py` and prints, per set: commits, simulated seconds, seconds per
  commit, misses, CPU seconds, and the ten commits with the largest (cost − best packing
  of the true failures), with that commit's reason from the selector's stderr.
