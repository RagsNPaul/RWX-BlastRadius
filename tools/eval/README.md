# Evaluation tools (paths are hard-coded to the machine they were built on; parametrise first)

- gt.py + gtplug.py: TRUE failing tests for any patch. Copies sqlglot, applies the patch, runs the full
  suite (`-n 2`, SKIP_INTEGRATION=0, --timeout=120) with a pytest plugin that records FAILED, ERROR,
  SUBFAILED and collection errors (= every test in that file). Validated: reproduces sample answers
  exactly for rszg, 986c, 2jrq, 66ks. About 80 s per patch on 2 cores.
- mutate.py SEED N OUTDIR: N random one-line mutants (operators, booleans, numbers, string keys) on
  lines tests run AND on import-time/class-level lines, across all of sqlglot.
- ev.py SAMPLE_OUT BANK_OUT: simulated seconds + misses for samples and bank.

# Map additions (tools/)
- conftest_inst.py: inline ProcessPool (calls initializer!) + records which sqlglot classes each test
  instantiates (with MRO) → inst.json.
- import_classes.py: classes instantiated while importing sqlglot (template parses) → import_classes.json.

# bank/
24 mutants (seed 7) + truth.json (true failing test numbers).
Results: cofounder selector 2 misses (r7_06, r7_11); prototype/reference_selector.py (V=B) 0 misses.
