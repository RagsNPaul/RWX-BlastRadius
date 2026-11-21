#!/usr/bin/env python3
"""Coverage data + conftest traces -> maps/. Run by tools/build_map.sh from the repo root.

maps/sets.json          [[test numbers], ...]: each distinct set of tests that ran some line
maps/lines/<path>.json  {line: index into sets.json}: tests that ran the statement the line
                        belongs to (see statement_lines)
maps/index.json
    durations     [milliseconds], indexed by test number
    file_union    {path: tests that ran any line of the file, plus tests that use the module
                   when it is imported lazily (maps/trace.json "users")}
    import_lines  {path: lines run with no test active, i.e. at import}
    opens         {path: tests that opened this non-.py file} (maps/opens.json)
    py_reads      {path: tests that read this .py file as text} (maps/trace.json)
    testfile      {test file: its tests}
"""
import ast
import json
import math
import os
import sys
from collections import defaultdict

from coverage import CoverageData

SQ, MAPS, LIMIT = "sqlglot", "maps", 15 * 1024 * 1024


def log(*message):
    print("make_map:", *message, file=sys.stderr)


def die(*message):
    log(*message)
    sys.exit(1)


def load(path):
    if not os.path.exists(path):
        die(path, "is missing: the test run did not write it")
    with open(path) as f:
        return json.load(f)


def dump(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, sort_keys=True, separators=(",", ":"))


def context(test_id):  # tests/x.py::TestY::test_z -> tests.x.TestY.test_z
    file, _, rest = test_id.partition("::")
    return file[:-3].replace("/", ".") + "." + rest.replace("::", ".")


suite = load("sample/tests.json")
tests = suite["tests"]
by_context = {context(t["id"]): t["number"] for t in tests}
skipped = {context(test_id) for test_id in suite.get("skipped", [])}
everything = {t["number"] for t in tests}

by_scope = defaultdict(set)  # test id, file::Class and file -> tests
for t in tests:
    parts = t["id"].split("::")
    for size in range(1, len(parts) + 1):
        by_scope["::".join(parts[:size])].add(t["number"])
unknown_scopes = set()


def expand(scopes):
    selected = set()
    for scope in scopes:
        if scope == "":
            selected |= everything
        elif scope in by_scope or scope.split("[")[0] in by_scope:
            selected |= by_scope.get(scope) or by_scope[scope.split("[")[0]]
        else:
            unknown_scopes.add(scope)
    return selected


def statement_lines(source):
    """Groups of lines that run as one unit: a simple statement, the header of a compound one
    (up to the line before its body), or a lambda in import-time code. Innermost unit wins.

    Coverage stores the lines that really ran. In `if (\\n a\\n and b\\n):` the `if (` line never
    appears, and a test that stops at `a` is not on the `and b` line, yet changing that line can
    break it. Constant lines inside a multi-line literal are not recorded either.
    """
    owner, units = {}, 0

    def start(node):
        return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", ())])

    def visit(node, in_function):
        nonlocal units
        for child in ast.iter_child_nodes(node):
            is_statement = isinstance(child, (ast.stmt, ast.ExceptHandler))
            if is_statement or (isinstance(child, ast.Lambda) and not in_function):
                body = getattr(child, "body", None) if is_statement else None
                last = max(start(child), start(body[0]) - 1) if body else child.end_lineno
                units += 1
                for line in range(start(child), last + 1):
                    owner[line] = units
            is_function = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            visit(child, in_function or is_function)

    visit(ast.parse(source), False)
    groups = defaultdict(list)
    for line, unit in owner.items():
        groups[unit].append(line)
    return groups.values()


# --- coverage: line -> tests, import-time lines, tests per file ---------------------------
data = CoverageData(".coverage")
data.read()
lines, import_lines, union, unmapped = {}, {}, {}, set()
for file in sorted(data.measured_files()):
    rel = os.path.relpath(file, SQ).replace(os.sep, "/")
    per_line, at_import = {}, set()
    for line, contexts in sorted(data.contexts_by_lineno(file).items()):
        ran = {by_context[c] for c in contexts if c in by_context}
        unmapped.update(c for c in contexts if c and c not in by_context)
        if "" in contexts:
            at_import.add(line)
        if ran:
            per_line[line] = ran
    with open(file, encoding="utf-8") as f:
        for group in statement_lines(f.read()):  # every line of a unit gets the unit's tests
            ran = set().union(*(per_line.get(line, ()) for line in group))
            if ran:
                per_line.update(dict.fromkeys(group, ran))
            if not at_import.isdisjoint(group):
                at_import.update(group)
    if per_line:
        lines[rel] = per_line
        union[rel] = set().union(*per_line.values())
    if at_import:
        import_lines[rel] = sorted(at_import)

log("unmapped ctx", len(unmapped), sorted(unmapped)[:5])
# The pinned image skips the SQLite test, so it never becomes a context. On a machine
# where that test runs, it is the one unmapped context. Anything else means the run broke.
if not unmapped <= skipped:
    die("unmapped context is not the skipped test", sorted(unmapped - skipped), "- the run is broken")
covered = set().union(*union.values())
log("tests that ran no sqlglot line:", [t["id"] for t in tests if t["number"] not in covered])

# --- traces written by tools/conftest_map.py ----------------------------------------------
opens = {path: sorted(expand(scopes)) for path, scopes in load(f"{MAPS}/opens.json").items()}
if not any(path.startswith("tests/fixtures/") for path in opens):
    die("maps/opens.json names no file under tests/fixtures: open tracing is broken")
trace = load(f"{MAPS}/trace.json")
py_reads = {path: sorted(expand(scopes)) for path, scopes in trace["py_reads"].items()}
for scope, used in trace["users"].items():
    numbers = expand([scope])
    for path in (trace["modules"][i] for i in used):
        union[path] = union.get(path, set()) | numbers
if "" in trace["users"]:
    log("modules used outside any test (credited to every test):", [trace["modules"][i] for i in trace["users"][""]])
log("scopes that are not numbered tests:", sorted(unknown_scopes))

# --- write --------------------------------------------------------------------------------
sets, index_of, written = [], {}, set()
for rel, per_line in sorted(lines.items()):
    table = {}
    for line, ran in sorted(per_line.items()):
        key = tuple(sorted(ran))
        if key not in index_of:
            index_of[key] = len(sets)
            sets.append(key)
        table[line] = index_of[key]
    dump(f"{MAPS}/lines/{rel}.json", table)
    written.add(os.path.normpath(f"{MAPS}/lines/{rel}.json"))
dump(f"{MAPS}/sets.json", sets)

testfile = defaultdict(list)
for t in tests:
    testfile[t["file"]].append(t["number"])
dump(
    f"{MAPS}/index.json",
    {
        "durations": [math.floor(t["seconds"] * 1000 + 0.5) for t in tests],
        "file_union": {path: sorted(numbers) for path, numbers in union.items()},
        "import_lines": import_lines,
        "opens": opens,
        "py_reads": py_reads,
        "testfile": testfile,
    },
)

if os.path.exists(f"{MAPS}/map.pkl"):
    os.remove(f"{MAPS}/map.pkl")  # the old pickle must not be loaded by mistake
sizes = {}
for folder, _, names in os.walk(MAPS):
    for name in names:
        path = os.path.normpath(os.path.join(folder, name))
        if path.startswith(os.path.normpath(f"{MAPS}/lines") + os.sep) and path not in written:
            die(path, "is left over from an earlier build; remove it and rerun")
        sizes[path] = os.path.getsize(path)
total = sum(sizes.values())
log(
    f"map bytes {total} ({total / 1e6:.2f} MB): {len(written)} line files, {len(sets)} sets,",
    f"sets.json {sizes[os.path.normpath(f'{MAPS}/sets.json')]}, index.json {sizes[os.path.normpath(f'{MAPS}/index.json')]}",
)
if total >= LIMIT:
    die("maps/ is over the 15 MB limit")
