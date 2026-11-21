"""Optimize fixture queries on the patched package.

python -S, cwd /tmp, PYTHONPATH is the patched tree. stdin is
{"schemas": {...}, "queries": [{"id", "sql", "schema"}]}.
stdout is one JSON object, query id to the sha256 of optimize().sql(),
or null when that query fails. The call matches tests/test_executor.py:
optimize(sql, schema, leave_tables_isolated=True).
"""
import hashlib
import json
import sys


def main():
    job = json.load(sys.stdin)
    from sqlglot.optimizer import optimize

    schemas = job["schemas"]
    out = {}
    for query in job["queries"]:
        try:
            text = optimize(query["sql"], schemas[query["schema"]], leave_tables_isolated=True).sql()
            out[query["id"]] = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        except Exception:
            out[query["id"]] = None
    json.dump(out, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
