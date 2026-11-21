# v2 prompts: from 288 s to the efficient phase

## Where we are (board, ~40 commits)
- Latest: **288.3 s simulated + 0.91 s CPU, 0 misses** = 7.2 s/commit.
- Best possible ≈ 3.2 s/commit (≈ 126 s for 40). The leader is ≈ 3.6 s/commit.
- The board has ~40 commits (1614.1 s baseline ÷ 40.35 s per starter commit). Always compare **per commit**, never totals against the 30 samples.

## Rules for every prompt below (paste once at the start of a Cursor session)
> Hard rules for this repo: (1) Zero misses beats every saving: one miss = 300 s. (2) A rule may only get narrower if `tools/eval/ev.py` shows 0 misses on sample/ AND on every patch in bank/ with ground truth, including new targeted patches for that rule. (3) One rule change per submission; log every run in RESULTS.md (git hash, sample s, sample s/commit, bank misses, bank s/commit, CPU s, board s). (4) No table from a patch or a kind of edit to the tests it breaks; maps from the pinned commit only. (5) Runtime stays stdlib-only, under 1 s CPU, and never crashes: on any error, that commit gets the full suite. (6) When unsure, select more.

---

## Prompt 0: tooling first (do once, ~30 min)
> Make tools/eval/ portable: replace every hard-coded /home/claude path in gt.py, gtplug.py, mutate.py, ev.py with CLI flags (--sqlglot, --python, --tests, --bank, --maps). gt.py must run pytest with `-n auto`, `SKIP_INTEGRATION=0`, `--timeout=120`, `-p no:cacheprovider`, `-p gtplug`, and write bank/<id>.json next to each patch (skip patches that already have one). ev.py reads sample answers + every bank/<id>.json (and bank/truth.json), runs selector/main.py, and prints per set: commits, simulated s, **s/commit**, misses with ids, CPU s, plus the top 10 commits by (cost − best possible) with each one's reasons from stderr. Add `make eval` running both. Verify gt.py reproduces sample/answers.json exactly for rszg, 2jrq, 66ks and 986c before anything else.

## Prompt 1: grow the test-patch bank (start it, let it run in the background)
> Extend tools/eval/mutate.py with `--kind`: `random` (current), `classattr` (class-level assignments in sqlglot/expressions, parsers, generators, dialects, tokens.py), `enumcmp` (swap `is`/`==` and `is not`/`!=` where one side is `TokenType.X`, another Enum member, or `None`), `copyflag` (`copy=True`↔`copy=False`), `deadbranch` (edit a line inside an if/elif/else body that the map says no test runs, while its condition line IS run), `fixture` (corrupt one token in one line of tests/fixtures/**), `comment` (insert a comment line anywhere, including inside multi-line statements and above docstrings). Generate seed 8: 30 random + 15 classattr + 15 enumcmp + 10 copyflag + 10 deadbranch + 5 fixture + 5 comment = 90 patches. Run gt.py on all of them in the background (about 1.5–2 h). Never reuse an id; never delete a patch.

## Prompt 2: port the class-level rules (required before any narrowing; may already be done)
> Check selector/rules.py against prototype/reference_selector.py (variant B). It must contain: (a) class-level edit inside class C → tests that instantiated C or a subclass (prototype/inst.json, keyed `module:QualName`, MRO-expanded), unioned with file_union; (b) if C is in prototype/import_classes.json → all tests; (c) a changed line inside a function that also runs at import (import_lines) → all tests. Move inst.json and import_classes.json into maps/ (built by tools/conftest_inst.py and tools/import_classes.py). Run `make eval`: 0 misses on sample and bank, including r7_06 and r7_11.

## Prompt 3: narrow class-level edits (≈ 20–35 s on the board)
> Behind a flag NARROW_CLASS=1: for a class-level edit inside class C that is NOT created during import, select only tests that instantiated C or a subclass (inst map), plus tests whose source or fixture SQL contains any string literal that the edit removed or added (case-insensitive, searched offline into a word→tests index in maps/). Drop the file_union fallback in that case; keep file_union only when the inst map has no entry for C. Module-level (not in a class) assignments keep file_union. Run `make eval` with the flag on and off and show the per-commit diff. Keep it on only if misses stay 0 on sample + bank (especially every `classattr` patch) and s/commit drops.

## Prompt 4: harmless-edit rules, one at a time (≈ 30–60 s on the board)
Run these as separate prompts and separate submissions. Each one makes a patch cost **0 s** instead of its full selection.

**4a: enum comparisons**
> Behind EQUIV_ENUM=1: a patch whose only AST difference (compare old/new ast.dump per changed statement) swaps `is`↔`==` or `is not`↔`!=`, where one operand is an attribute of an Enum subclass (resolve `TokenType.X` etc. by importing nothing at runtime: precompute offline into maps/ the set of Enum classes in sqlglot and whether any of them override `__eq__`), counts as no change for that statement. Do NOT apply to `None` yet. Run `make eval`; it must show 0 misses on all `enumcmp` patches and the rest.

**4b: `None` comparisons**
> Same as 4a for `is None`↔`== None`, but only when the other operand can't be an object that overrides `__eq__`. sqlglot's Expression overrides `__eq__`, so this only applies when the variable is clearly a str/int/bool/Token/TokenType (annotation or obvious assignment in the same function). If unsure, don't apply. Gate on the `enumcmp` None patches.

**4c: dead branches**
> Behind EQUIV_DEAD=1: if every changed line sits inside an if/elif/else/except/loop body whose lines no test runs (no map entry, not in import_lines), while the edit does not touch the condition line itself, select nothing for that file. Gate on the `deadbranch` patches.

**4d: copy flags**
> Behind EQUIV_COPY=1: `copy=True`↔`copy=False` on a call whose result is assigned and returned without further mutation in the same function. Be conservative. Gate on the `copyflag` patches. If any of them shows a miss, drop the rule.

## Prompt 5: expensive-test audit (find the next lever)
> Add to ev.py a report: for every commit in sample + bank, list the selected tests that take over 3 s (test_max_nodes, test_execute_tpcds, test_execute_tpch, test_generator_memory, ...), which rule and which line pulled each one in, and whether it actually failed. Sort by seconds that were wasted (selected but passed). Print the 10 (rule, line-range) pairs that waste the most. Don't change selection logic in this prompt.

## Prompt 6: exact packing (small, safe)
> In pack.py: for selections of 12 or fewer tests, search all assignments to k = 1..8 workers exhaustively (prune by current best); otherwise keep LPT plus one pass of pairwise swaps between the busiest worker and each other. Confirm with ev.py that s/commit never gets worse on any commit.

---

## Order and gates
1. Prompt 0 → Prompt 1 (bank runs in the background) → Prompt 2 → **submit** (confirms 0 misses still).
2. Prompt 3 → `make eval` → submit if 0 misses and s/commit dropped.
3. Prompt 4a → submit; 4c → submit; 4b and 4d only if their targeted patches are clean.
4. Prompt 5 → pick the biggest remaining waste → write a rule for it the same way (flag, targeted patches, gate).
5. Prompt 6 any time.

Projection: to compare with the board, multiply your bank's s/commit by 40 (the samples are easier than the board).
Board target after 3 + 4a + 4c: about 190–230 s. Getting below that comes from whatever Prompt 5 finds.
