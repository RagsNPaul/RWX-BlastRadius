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
- `ev.py --sqlglot DIR --python PY --tests sample/tests.json --bank bank --maps maps`
  Reads `sample/answers.json`, every `bank/<id>.json`, and `bank/truth.json`.
  Runs `selector/main.py` and prints, per set: commits, simulated seconds, seconds per
  commit, misses, CPU seconds, and the ten commits with the largest (cost − best packing
  of the true failures), with that commit's reason from the selector's stderr.
