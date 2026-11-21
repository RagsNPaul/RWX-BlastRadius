"""Turn a coverage.py DB (with test_function contexts) into maps/map.json.gz."""

from __future__ import annotations

import collections
import gzip
import json
import os
import pickle
import sys

from coverage import CoverageData

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)

tests = json.load(open("sample/tests.json"))["tests"]
ctxmap: dict[str, int] = {}
for t in tests:
    path, _, rest = t["id"].partition("::")
    cls, _, fn = rest.partition("::")
    ctxmap[path[:-3].replace("/", ".") + f".{cls}.{fn}"] = t["number"]

data = CoverageData(".coverage")
data.read()

lines: dict[str, dict[int, set[int]]] = collections.defaultdict(
    lambda: collections.defaultdict(set)
)
imp: dict[str, set[int]] = collections.defaultdict(set)
filetests: dict[str, set[int]] = collections.defaultdict(set)
unk: set[str] = set()

for path in data.measured_files():
    rel = os.path.relpath(path, "sqlglot")
    for ln, ctxs in data.contexts_by_lineno(path).items():
        for ctx in ctxs:
            if ctx == "":
                imp[rel].add(ln)
                continue
            number = ctxmap.get(ctx)
            if number is None:
                unk.add(ctx)
                continue
            lines[rel][ln].add(number)
            filetests[rel].add(number)

print("unmapped ctx", len(unk), list(unk)[:3], file=sys.stderr)

os.makedirs("maps", exist_ok=True)
payload = {
    "lines": {f: {str(ln): sorted(ts) for ln, ts in by_line.items()} for f, by_line in lines.items()},
    "import_lines": {f: sorted(lns) for f, lns in imp.items()},
    "file_tests": {f: sorted(ts) for f, ts in filetests.items()},
}
with gzip.open("maps/map.json.gz", "wt", encoding="utf-8") as fh:
    json.dump(payload, fh, separators=(",", ":"))

# Keep pickle for the prototype / local iteration.
pickle.dump(
    ({k: dict(v) for k, v in lines.items()}, dict(imp), dict(filetests)),
    open("maps/map.pkl", "wb"),
)
print("wrote maps/map.json.gz and maps/map.pkl", file=sys.stderr)
