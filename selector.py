"""Change-aware test selector for Blast Radius.

Maps each patch to likely-failing tests via a committed coverage map, AST
classification (noop / class-level / and-or flip), fixtures, and test-file rules.
"""

from __future__ import annotations

import ast
import gzip
import json
import os
import pickle
import re
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent
DATA = Path(os.environ.get("BLAST_DATA", "/data"))
SQLGLOT = APP / "sqlglot"
MAP_PATHS = (APP / "maps" / "map.json.gz", APP / "maps" / "map.pkl")
WORKER_COST_MS = 2000
MAX_WORKERS = 8

BROAD_FILES = (
    "tests/test_optimizer.py",
    "tests/test_executor.py",
    "tests/test_serde.py",
    "tests/test_expressions.py",
)
# file_tests larger than this are treated as "imported by everyone" and ignored
# for expansion (e.g. dialects/dialect.py).
FILETESTS_CAP = 80


def load_map() -> tuple[dict, dict, dict]:
    for path in MAP_PATHS:
        if not path.exists():
            continue
        if path.suffix == ".gz":
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                raw = json.load(fh)
            lines = {
                f: {int(ln): set(tests) for ln, tests in by_line.items()}
                for f, by_line in raw["lines"].items()
            }
            imp = {f: set(lns) for f, lns in raw["import_lines"].items()}
            filetests = {f: set(tests) for f, tests in raw["file_tests"].items()}
            return lines, imp, filetests
        with path.open("rb") as fh:
            return pickle.load(fh)
    raise SystemExit(f"coverage map not found under {APP / 'maps'}")


def parse_patch(text: str) -> dict[str, list]:
    out: dict[str, list] = {}
    cur: str | None = None
    old_line = 0
    for line in text.splitlines():
        if line.startswith("+++ "):
            cur = line[6:] if line.startswith("+++ b/") else line[4:]
            if cur.startswith("b/"):
                cur = cur[2:]
            out[cur] = []
        elif line.startswith("@@") and cur is not None:
            match = re.match(r"@@ -(\d+)", line)
            if not match:
                continue
            old_line = int(match.group(1))
            out[cur].append(["h"])
        elif cur and out[cur] and line[:1] in " -+":
            kind = line[0]
            out[cur][-1].append((kind, old_line, line[1:]))
            if kind != "+":
                old_line += 1
    return out


def apply_hunks(src: str, hunks: list) -> str:
    lines = src.split("\n")
    result: list[str] = []
    pos = 1
    for hunk in hunks:
        rows = hunk[1:]
        if not rows:
            continue
        start = rows[0][1]
        result.extend(lines[pos - 1 : start - 1])
        pos = start
        for kind, _old, text in rows:
            if kind == " ":
                result.append(lines[pos - 1])
                pos += 1
            elif kind == "-":
                pos += 1
            else:
                result.append(text)
    result.extend(lines[pos - 1 :])
    return "\n".join(result)


def statement_starts(src: str) -> dict[int, int]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    mapping: dict[int, int] = {}
    nodes: list[ast.AST] = [
        node
        for node in ast.walk(tree)
        if hasattr(node, "lineno")
        and hasattr(node, "end_lineno")
        and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module))
    ]
    # Smaller spans overwrite larger ones so dict/lambda entries keep tight starts.
    nodes.sort(key=lambda n: (n.end_lineno or n.lineno) - n.lineno, reverse=True)
    for node in nodes:
        start = node.lineno
        end = node.end_lineno or start
        for ln in range(start, end + 1):
            mapping[ln] = start
    nodes.sort(key=lambda n: (n.end_lineno or n.lineno) - n.lineno)
    for node in nodes:
        start = node.lineno
        end = node.end_lineno or start
        if end - start > 40:
            continue
        for ln in range(start, end + 1):
            mapping[ln] = start
    return mapping


def changed_old_lines(hunks: list) -> set[int]:
    changed: set[int] = set()
    for hunk in hunks:
        rows = hunk[1:]
        has_delete = any(kind == "-" for kind, _, _ in rows)
        for kind, old, _text in rows:
            if kind == "-":
                changed.add(old)
            elif kind == "+" and not has_delete:
                if old > 1:
                    changed.add(old - 1)
                changed.add(old)
    return changed


def pack(selected: set[int], durations_ms: list[int]) -> list[list[int]]:
    if not selected:
        return []
    order = sorted(selected, key=lambda i: -durations_ms[i])
    best: tuple[int, list[list[int]]] | None = None
    for k in range(1, MAX_WORKERS + 1):
        workers: list[list[int]] = [[] for _ in range(k)]
        loads = [0] * k
        for test in order:
            lightest = loads.index(min(loads))
            workers[lightest].append(test)
            loads[lightest] += durations_ms[test]
        cost = max(loads) + WORKER_COST_MS * k
        if best is None or cost < best[0]:
            best = (cost, workers)
    assert best is not None
    return [w for w in best[1] if w]


def fixture_tests(tests: list[dict], sqlglot_root: Path) -> dict[int, str]:
    numbers = {t["id"]: t["number"] for t in tests}
    bodies: dict[int, str] = {}
    for path in (sqlglot_root / "tests").rglob("*.py"):
        rel = str(path.relative_to(sqlglot_root))
        try:
            src = path.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            for fn in node.body:
                if not isinstance(fn, ast.FunctionDef):
                    continue
                number = numbers.get(f"{rel}::{node.name}::{fn.name}")
                if number is None:
                    continue
                segment = ast.get_source_segment(src, fn)
                if segment:
                    bodies[number] = segment
    return bodies


def tests_for_fixture(fixture_path: str, bodies: dict[int, str]) -> set[int]:
    stem = fixture_path[len("tests/fixtures/") :]
    if stem.endswith(".sql"):
        stem = stem[:-4]
    base = stem.split("/")[-1]
    hit: set[int] = set()
    for number, src in bodies.items():
        if f'"{base}"' in src or f"'{base}'" in src or stem in src:
            hit.add(number)
    return hit


def tests_in_file(tests: list[dict], rel_path: str) -> set[int]:
    return {t["number"] for t in tests if t["file"] == rel_path}


def tests_in_files(tests: list[dict], files: tuple[str, ...] | list[str]) -> set[int]:
    wanted = set(files)
    return {t["number"] for t in tests if t["file"] in wanted}


def safe_filetests(filetests: dict, path: str) -> set[int]:
    hits = set(filetests.get(path, ()))
    if len(hits) > FILETESTS_CAP:
        return set()
    return hits


def enclosing_function_lines(tree: ast.AST) -> set[int]:
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = node.end_lineno or start
            inside.update(range(start, end + 1))
    return inside


def top_level_class_direct_assign_names(tree: ast.AST, lines: set[int]) -> set[str]:
    """Names assigned directly on a module-level class (not nested classes)."""
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for child in node.body:
            if not isinstance(child, (ast.Assign, ast.AnnAssign)):
                continue
            start = child.lineno
            end = getattr(child, "end_lineno", start) or start
            if not lines.intersection(range(start, end + 1)):
                continue
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def and_or_flip(old_src: str, new_src: str) -> bool:
    """True only when And/Or nesting flips — ignores compare/is tweaks inside."""
    try:
        old_t, new_t = ast.parse(old_src), ast.parse(new_src)
    except SyntaxError:
        return False

    def skeleton(node: ast.AST):
        if isinstance(node, ast.BoolOp):
            return (type(node.op).__name__, tuple(skeleton(v) for v in node.values))
        return "_"

    def boolops(tree: ast.AST) -> list[str]:
        return sorted(
            repr(skeleton(node))
            for node in ast.walk(tree)
            if isinstance(node, ast.BoolOp)
        )

    return boolops(old_t) != boolops(new_t)


def dialect_test_file(path: str) -> str | None:
    parts = Path(path).parts
    if len(parts) >= 3 and parts[0] == "sqlglot" and parts[1] in {
        "dialects",
        "parsers",
        "generators",
    }:
        stem = Path(parts[-1]).stem
        if stem == "dialect":
            return None
        return f"tests/dialects/test_{stem}.py"
    return None


def build_attr_readers(lines: dict) -> dict[str, set[int]]:
    """Map attribute name -> tests covering lines that mention dialect.ATTR / self.ATTR."""
    readers: dict[str, set[int]] = {}
    root = SQLGLOT / "sqlglot"
    if not root.exists():
        return readers
    pattern = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\b")
    for path in root.rglob("*.py"):
        rel = str(path.relative_to(SQLGLOT))
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        file_map = lines.get(rel, {})
        if not file_map:
            continue
        for lineno, raw in enumerate(text.splitlines(), 1):
            covering = file_map.get(lineno)
            if not covering:
                continue
            if "WEEK_OFFSET" in raw or "normalization_strategy" in raw:
                for name in pattern.findall(raw):
                    readers.setdefault(name, set()).update(covering)
            elif "dialect." in raw or "self.dialect." in raw:
                for name in pattern.findall(raw):
                    if name.endswith("_OFFSET") or name.endswith("_STRATEGY"):
                        readers.setdefault(name, set()).update(covering)
    return readers


def select_for_file(
    path: str,
    hunks: list,
    *,
    tests: list[dict],
    lines: dict,
    imp: dict,
    filetests: dict,
    bodies: dict[int, str],
    all_tests: set[int],
    attr_readers: dict[str, set[int]],
) -> set[int]:
    if path.startswith("tests/fixtures/"):
        return tests_for_fixture(path, bodies)

    if path.startswith("tests/") and path.endswith(".py"):
        name = Path(path).name
        if name == "conftest.py" or "helpers" in Path(path).parts:
            return set(all_tests)
        return tests_in_file(tests, path)

    if not (path.endswith(".py") and path.startswith("sqlglot/")):
        return set(all_tests)

    source_path = SQLGLOT / path
    if not source_path.exists():
        return set(all_tests)

    old = source_path.read_text(encoding="utf-8")
    new = apply_hunks(old, hunks)
    try:
        old_tree = ast.parse(old)
        new_tree = ast.parse(new)
        if ast.dump(old_tree) == ast.dump(new_tree):
            return set()
    except SyntaxError:
        return set(all_tests)

    selected: set[int] = set()
    changed = changed_old_lines(hunks)
    starts = statement_starts(old)
    inside_fn = enclosing_function_lines(old_tree)
    class_level_lines = {ln for ln in changed if ln not in inside_fn}
    file_map = lines.get(path, {})
    uncovered = False

    for ln in changed:
        covering: set[int] = set()
        for key in (ln, starts.get(ln, ln), ln - 1, ln + 1):
            if key in file_map:
                covering |= set(file_map[key])
        if covering:
            selected |= covering
        elif ln in imp.get(path, ()) or starts.get(ln, ln) in imp.get(path, ()):
            selected |= safe_filetests(filetests, path)
        else:
            uncovered = True

    if uncovered:
        selected |= safe_filetests(filetests, path)

    # Top-level dialect class attributes (WEEK_OFFSET, etc.): import-time, cross-cutting.
    top_names = top_level_class_direct_assign_names(old_tree, class_level_lines)
    top_names |= top_level_class_direct_assign_names(new_tree, class_level_lines)
    if top_names and path.startswith("sqlglot/dialects/"):
        selected |= safe_filetests(filetests, path)
        dtype = dialect_test_file(path)
        if dtype:
            selected |= tests_in_file(tests, dtype)
        for name in top_names:
            selected |= attr_readers.get(name, set())
        # Fallback if the attr was never seen under coverage.
        if not any(attr_readers.get(name) for name in top_names):
            selected |= tests_in_file(tests, "tests/test_optimizer.py")

    # and/or flips change which branch runs; coverage under-approximates badly.
    if and_or_flip(old, new):
        selected |= safe_filetests(filetests, path)
        if path.startswith("sqlglot/serde") or path.startswith("sqlglot/optimizer/"):
            selected |= tests_in_files(tests, BROAD_FILES)
        elif path.startswith("sqlglot/typing/"):
            # Typing tables are narrowly covered; file_tests is enough.
            selected |= safe_filetests(filetests, path)
        else:
            # parsers / generators / dialects: broaden, but skip heavy executor pools.
            selected |= tests_in_files(
                tests,
                (
                    "tests/test_optimizer.py",
                    "tests/test_serde.py",
                    "tests/test_expressions.py",
                ),
            )

    if path.startswith("sqlglot/typing/"):
        selected |= safe_filetests(filetests, path)

    dtype = dialect_test_file(path)
    if dtype and selected:
        selected |= tests_in_file(tests, dtype)

    # Real AST change with no mapped tests: keep a small safety net.
    if not selected:
        selected |= safe_filetests(filetests, path)
        if dtype:
            selected |= tests_in_file(tests, dtype)
        if not selected and path.startswith("sqlglot/expressions/"):
            selected |= tests_in_file(tests, "tests/test_expressions.py")
        if not selected and (
            path.startswith("sqlglot/optimizer/")
            or path.startswith("sqlglot/serde")
            or path.startswith("sqlglot/typing/")
        ):
            selected |= tests_in_files(tests, BROAD_FILES)

    return selected


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text(encoding="utf-8"))["tests"]
    durations_ms = [round(t["seconds"] * 1000) for t in tests]
    all_tests = {t["number"] for t in tests}
    lines, imp, filetests = load_map()
    bodies = fixture_tests(tests, SQLGLOT)
    attr_readers = build_attr_readers(lines)

    for patch_path in sorted((DATA / "commits").glob("*.patch")):
        selected: set[int] = set()
        reasons: list[str] = []
        for path, hunks in parse_patch(patch_path.read_text(encoding="utf-8")).items():
            hit = select_for_file(
                path,
                hunks,
                tests=tests,
                lines=lines,
                imp=imp,
                filetests=filetests,
                bodies=bodies,
                all_tests=all_tests,
                attr_readers=attr_readers,
            )
            selected |= hit
            if not hit and path.startswith("sqlglot/"):
                reasons.append("noop")
            elif path.startswith("tests/fixtures/"):
                reasons.append("fixture")
            elif path.startswith("tests/"):
                reasons.append("testfile")
            elif len(hit) == len(all_tests):
                reasons.append("ALL")
            else:
                reasons.append(f"code:{len(hit)}")

        workers = pack(selected, durations_ms)
        print(json.dumps({"commit": patch_path.stem, "workers": workers}))
        print(
            f"{patch_path.stem} tests={len(selected)} "
            f"s={sum(durations_ms[i] for i in selected) / 1000:.1f} {reasons}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
