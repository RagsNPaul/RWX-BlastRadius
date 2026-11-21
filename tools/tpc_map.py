"""Build the offline maps for EXPENSIVE_CHECK from the pinned checkout.

maps/tpc.json: each TPC-H / TPC-DS fixture query, the sha256 of optimize().sql()
with the schema and leave_tables_isolated=True that tests/test_executor.py uses,
and the sqlglot files that query actually calls.

maps/max_nodes_lines.json: lines test_max_nodes runs that also sit on the default
dialect's parse path (parse_one with no dialect, including the node-limit error).
"""
import hashlib
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_NODES = 1490

if os.environ.get("PYTHONHASHSEED") != "0":
    os.environ["PYTHONHASHSEED"] = "0"
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)] + sys.argv[1:])

sys.path[:0] = [os.path.join(REPO, "sqlglot"), os.path.join(REPO, "sqlglot", "tests")]


def _queries():
    from helpers import TPCDS_SCHEMA, TPCH_SCHEMA, load_sql_fixture_pairs

    found = []
    for suite, schema, name in (
        ("h", TPCH_SCHEMA, "optimizer/tpc-h/tpc-h.sql"),
        ("d", TPCDS_SCHEMA, "optimizer/tpc-ds/tpc-ds.sql"),
    ):
        for index, (_meta, sql, _expected) in enumerate(load_sql_fixture_pairs(name)):
            found.append((f"{suite}{index}", suite, sql))
    return found, {"h": TPCH_SCHEMA, "d": TPCDS_SCHEMA}


def _files_and_hash(schema, sql):
    from sqlglot.optimizer import optimize

    root = os.path.join(REPO, "sqlglot", "sqlglot") + os.sep
    base = os.path.join(REPO, "sqlglot")
    seen = set()

    def profile(frame, event, _arg):
        if event == "call":
            name = frame.f_code.co_filename
            if name.startswith(root) and name.endswith(".py"):
                seen.add(os.path.relpath(name, base))

    sys.setprofile(profile)
    try:
        text = optimize(sql, schema, leave_tables_isolated=True).sql()
    finally:
        sys.setprofile(None)
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    return sorted(seen), digest


def _parse_lines():
    from sqlglot import parse_one

    root = os.path.join(REPO, "sqlglot", "sqlglot") + os.sep
    base = os.path.join(REPO, "sqlglot")
    hit = set()

    def trace(frame, event, _arg):
        if event == "line":
            name = frame.f_code.co_filename
            if name.startswith(root) and name.endswith(".py"):
                hit.add(f"{os.path.relpath(name, base)}:{frame.f_lineno}")
        return trace

    samples = [
        ("SELECT 1", {"max_nodes": 1_000_000}),
        ("SELECT " + ",".join(f"col{i}" for i in range(40)) + " FROM t", {"max_nodes": 1_000_000}),
        ("SELECT " + ",".join(["x" + "=x" * 30] * 20), {"max_nodes": 80}),
        ("SELECT 1, 2, 3", {}),
        ("WITH c AS (SELECT a FROM t) SELECT a FROM c JOIN u ON c.a = u.a WHERE a > 1", {}),
        ("SELECT SUM(a) OVER (PARTITION BY b ORDER BY c) FROM t", {}),
    ]
    for sql, kwargs in samples:
        sys.settrace(trace)
        try:
            try:
                parse_one(sql, **kwargs)
            except Exception:
                pass
        finally:
            sys.settrace(None)
    return hit


def _max_nodes_lines(parse_lines):
    sets = json.load(open(os.path.join(REPO, "maps", "sets.json"), encoding="utf-8"))
    ran = {index for index, numbers in enumerate(sets) if MAX_NODES in numbers}
    lines_dir = os.path.join(REPO, "maps", "lines")
    found = []
    for dirpath, _dirs, names in os.walk(lines_dir):
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.relpath(os.path.join(dirpath, name), lines_dir)[: -len(".json")]
            table = json.load(open(os.path.join(dirpath, name), encoding="utf-8"))
            for line, index in table.items():
                key = f"{path}:{line}"
                if index in ran and key in parse_lines:
                    found.append(key)
    return sorted(found)


def main():
    queries, schemas = _queries()
    files = {}
    stored = {}
    for number, (qid, suite, sql) in enumerate(queries, start=1):
        called, digest = _files_and_hash(schemas[suite], sql)
        stored[qid] = {"s": suite, "sql": sql, "hash": digest}
        for path in called:
            files.setdefault(path, []).append(qid)
        if number == 1 or number % 20 == 0 or number == len(queries):
            print(f"{number}/{len(queries)}", file=sys.stderr, flush=True)
    parse_lines = _parse_lines()
    max_lines = _max_nodes_lines(parse_lines)
    tpc_path = os.path.join(REPO, "maps", "tpc.json")
    line_path = os.path.join(REPO, "maps", "max_nodes_lines.json")
    with open(tpc_path, "w", encoding="utf-8") as handle:
        json.dump({"schemas": schemas, "queries": stored, "files": files}, handle, separators=(",", ":"))
        handle.write("\n")
    with open(line_path, "w", encoding="utf-8") as handle:
        json.dump(max_lines, handle, separators=(",", ":"))
        handle.write("\n")
    os.chmod(tpc_path, 0o644)
    os.chmod(line_path, 0o644)
    print(
        f"queries {len(stored)} files {len(files)} parse-lines {len(parse_lines)} max-nodes {len(max_lines)}",
        file=sys.stderr,
    )
    print(f"{os.path.getsize(tpc_path)} {os.path.getsize(line_path)}", file=sys.stderr)


if __name__ == "__main__":
    main()
