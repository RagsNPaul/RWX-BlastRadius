#!/usr/bin/env python3
"""One-line mutants of the pinned sqlglot tree.

  python3 tools/eval/mutate.py --sqlglot sqlglot --maps maps --kind random 8 30 bank

--kind chooses the edit. Ids are r<seed>_<kind>_<nn> and are never overwritten.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import random
import re
import sys
from pathlib import Path

KINDS = ("random", "classattr", "enumcmp", "copyflag", "deadbranch", "fixture", "comment")
OPS = [
    (r"==", r"!="),
    (r"!=", r"=="),
    (r" and ", r" or "),
    (r" or ", r" and "),
    (r" is not ", r" is "),
    (r" is (?!not)", r" is not "),
    (r"<=", r"<"),
    (r"(?<![<=])<(?![<=])", r"<="),
    (r">=", r">"),
    (r"(?<![>=-])>(?![>=])", r">="),
    (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"),
    (r"\bnot ", ""),
    (r"\+ 1\b", "- 1"),
    (r"- 1\b", "+ 1"),
    (r"\b0\b", "1"),
    (r"\b1\b", "2"),
    (r'"([A-Z_]{2,})"', r'"\1X"'),
]
ENUM_BASES = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
CLASSATTR_PREFIXES = (
    "sqlglot/expressions/",
    "sqlglot/parsers/",
    "sqlglot/generators/",
    "sqlglot/dialects/",
)
COMPARE_OPS = (
    (ast.IsNot, "is not", "!="),
    (ast.NotEq, "!=", "is not"),
    (ast.Is, "is", "=="),
    (ast.Eq, "==", "is"),
)
TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlglot", required=True, type=Path, help="sqlglot checkout to edit")
    parser.add_argument("--maps", required=True, type=Path, help="maps/ built from the pinned commit")
    parser.add_argument("--kind", default="random", choices=KINDS)
    parser.add_argument("seed", type=int)
    parser.add_argument("count", type=int)
    parser.add_argument("outdir", type=Path)
    return parser.parse_args()


def read_lines(path: Path) -> list[str]:
    return path.read_text().split("\n")


def python_files(root: Path) -> list[Path]:
    return sorted(path for path in (root / "sqlglot").rglob("*.py") if path.is_file())


def relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def parse_file(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text())
    except SyntaxError:
        return None


def dumps_same(before: str, after: str) -> bool | None:
    """True when both parse and the AST is unchanged. None when either fails to parse."""
    try:
        return ast.dump(ast.parse(before)) == ast.dump(ast.parse(after))
    except SyntaxError:
        return None


def covered_lines(maps: Path) -> dict[str, set[int]]:
    """Lines the pinned maps record as executed, by tests or at import."""
    found: dict[str, set[int]] = {}
    for path in (maps / "lines").rglob("*.json"):
        source = path.relative_to(maps / "lines").as_posix()[: -len(".json")]
        if source.startswith("sqlglot/"):
            found[source] = {int(line) for line in json.loads(path.read_text())}
    index = json.loads((maps / "index.json").read_text())
    for source, lines in index["import_lines"].items():
        if source.startswith("sqlglot/"):
            found.setdefault(source, set()).update(int(line) for line in lines)
    return found


def random_candidates(maps: Path) -> list[tuple[str, int]]:
    """Lines a test ran, then lines that ran at import."""
    found: list[tuple[str, int]] = []
    lines_root = maps / "lines"
    for path in sorted(lines_root.rglob("*.json")):
        source = path.relative_to(lines_root).as_posix()[: -len(".json")]
        if not source.startswith("sqlglot/"):
            continue
        for line in json.loads(path.read_text()):
            found.append((source, int(line)))
    index = json.loads((maps / "index.json").read_text())
    for source, lines in index["import_lines"].items():
        if not source.startswith("sqlglot/"):
            continue
        for line in lines:
            found.append((source, int(line)))
    return found


def classattr_file(source: str) -> bool:
    return source == "sqlglot/tokens.py" or source.startswith(CLASSATTR_PREFIXES)


def classattr_candidates(root: Path) -> list[tuple[str, int]]:
    """Every line of a class-body assignment in the requested packages."""
    found: list[tuple[str, int]] = []
    for path in python_files(root):
        source = relative(root, path)
        if not classattr_file(source):
            continue
        tree = parse_file(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for stmt in node.body:
                if not isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                    continue
                for line in range(stmt.lineno, stmt.end_lineno + 1):
                    found.append((source, line))
    return found


def enum_names(root: Path) -> set[str]:
    names = set()
    for path in python_files(root):
        tree = parse_file(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = set()
            for base in node.bases:
                if isinstance(base, ast.Name):
                    bases.add(base.id)
                elif isinstance(base, ast.Attribute):
                    bases.add(base.attr)
            if bases & ENUM_BASES:
                names.add(node.name)
    return names


def is_enum_side(node: ast.AST, enums: set[str]) -> bool:
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in enums
    )


def slice_text(lines: list[str], start: tuple[int, int], end: tuple[int, int]) -> str:
    """Source from (line, col) inclusive to (line, col) exclusive. Lines are 1-based."""
    first_line, first_col = start
    last_line, last_col = end
    if first_line == last_line:
        return lines[first_line - 1][first_col:last_col]
    parts = [lines[first_line - 1][first_col:]]
    parts.extend(lines[first_line: last_line - 1])
    parts.append(lines[last_line - 1][:last_col])
    return "\n".join(parts)


def replace_span(lines: list[str], start: tuple[int, int], end: tuple[int, int], new: str) -> list[str]:
    first_line, first_col = start
    last_line, last_col = end
    prefix = lines[first_line - 1][:first_col]
    suffix = lines[last_line - 1][last_col:]
    parts = (prefix + new + suffix).split("\n")
    return lines[: first_line - 1] + parts + lines[last_line:]


def enumcmp_candidates(root: Path) -> list[tuple[str, tuple]]:
    """Comparisons whose operator can swap, with one side an Enum member or None."""
    enums = enum_names(root)
    found = []
    for path in python_files(root):
        tree = parse_file(path)
        if tree is None:
            continue
        source = relative(root, path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or len(node.ops) != 1:
                continue
            comparator = node.comparators[0]
            if not (is_enum_side(node.left, enums) or is_enum_side(comparator, enums)):
                continue
            for op_type, old, new in COMPARE_OPS:
                if isinstance(node.ops[0], op_type):
                    found.append((source, (node.left, comparator, old, new)))
                    break
    return found


def copyflag_candidates(root: Path) -> list[tuple[str, ast.Constant]]:
    found = []
    for path in python_files(root):
        tree = parse_file(path)
        if tree is None:
            continue
        source = relative(root, path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                value = keyword.value
                if (
                    keyword.arg == "copy"
                    and isinstance(value, ast.Constant)
                    and value.value in (True, False)
                ):
                    found.append((source, value))
    return found


def deadbranch_candidates(root: Path, maps: Path) -> list[tuple[str, int]]:
    """A body line nothing executes, inside an if/elif/else whose condition does."""
    ran = covered_lines(maps)
    found = []
    for path in python_files(root):
        source = relative(root, path)
        tree = parse_file(path)
        if tree is None:
            continue
        lines = read_lines(path)
        executed = ran.get(source, set())
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            test_lines = set(range(node.test.lineno, node.test.end_lineno + 1))
            if not (test_lines & executed):
                continue
            statements = list(node.body)
            if node.orelse and not (len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If)):
                statements.extend(node.orelse)
            for stmt in statements:
                for line_no in range(stmt.lineno, stmt.end_lineno + 1):
                    if line_no in executed or line_no in test_lines:
                        continue
                    if line_no < 1 or line_no > len(lines):
                        continue
                    found.append((source, line_no))
    return found


def fixture_candidates(root: Path) -> list[tuple[str, int]]:
    found = []
    fixtures = root / "tests" / "fixtures"
    for path in sorted(fixtures.rglob("*")):
        if not path.is_file() or path.suffix not in {".sql", ".json"}:
            continue
        source = relative(root, path)
        try:
            lines = path.read_text().splitlines()
        except UnicodeError:
            continue
        for line_no, line in enumerate(lines, start=1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", "--")):
                continue
            if TOKEN.search(line):
                found.append((source, line_no))
    return found


def comment_sites(root: Path) -> dict[str, list[tuple[str, int]]]:
    """Where a comment line may be inserted: (source, line to insert before)."""
    multiline: list[tuple[str, int]] = []
    docstring: list[tuple[str, int]] = []
    anywhere: list[tuple[str, int]] = []
    for path in python_files(root):
        source = relative(root, path)
        lines = read_lines(path)
        depth, in_string, quote = 0, False, ""
        triple = False
        for index, line in enumerate(lines):
            if depth > 0 and not in_string and line.strip():
                multiline.append((source, index + 1))
            if depth == 0 and not in_string:
                anywhere.append((source, index + 1))
            i = 0
            while i < len(line):
                if in_string:
                    if triple and line.startswith(quote, i):
                        in_string = False
                        i += 3
                        continue
                    if not triple and line[i] == quote and (i == 0 or line[i - 1] != "\\"):
                        in_string = False
                    i += 1
                    continue
                if line[i] == "#":
                    break
                if line.startswith(("'''", '"""'), i):
                    in_string, triple, quote = True, True, line[i: i + 3]
                    i += 3
                    continue
                if line[i] in ("'", '"'):
                    in_string, triple, quote = True, False, line[i]
                    i += 1
                    continue
                if line[i] in "([{":
                    depth += 1
                elif line[i] in ")]}" and depth:
                    depth -= 1
                i += 1
        anywhere.append((source, len(lines) + 1))
        tree = parse_file(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            value = first.value if isinstance(first, ast.Expr) else None
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                docstring.append((source, first.lineno))
    return {"multiline": multiline, "docstring": docstring, "anywhere": anywhere}


def apply_ops(line: str, rng: random.Random) -> str | None:
    if line.strip().startswith(("#", '"""', "def ", "class ", "import", "from ", "@")):
        return None
    code = line.split("#")[0]
    ops = [op for op in OPS if re.search(op[0], code)]
    if not ops:
        return None
    pattern, replacement = rng.choice(ops)
    new = re.sub(pattern, replacement, line, count=1)
    return None if new == line else new


def edit_line(lines: list[str], line_no: int, rng: random.Random) -> list[str] | None:
    if line_no < 1 or line_no > len(lines):
        return None
    new = apply_ops(lines[line_no - 1], rng)
    if new is None:
        return None
    edited = lines[:]
    edited[line_no - 1] = new
    return edited


def corrupt_token(line: str, rng: random.Random) -> str | None:
    tokens = [match for match in TOKEN.finditer(line) if len(match.group()) > 1]
    if not tokens:
        return None
    match = rng.choice(tokens)
    return line[: match.end()] + "X" + line[match.end():]


def write_patch(outdir: Path, mutant: str, source: str, before: list[str], after: list[str]) -> None:
    path = outdir / f"{mutant}.patch"
    if path.exists():
        raise SystemExit(f"mutate: refusing to reuse existing id {mutant}")
    diff = "".join(
        difflib.unified_diff(
            [row + "\n" for row in before],
            [row + "\n" for row in after],
            f"a/{source}",
            f"b/{source}",
            n=3,
        )
    )
    path.write_text(f"diff --git a/{source} b/{source}\n" + diff)


def preview(before: list[str], after: list[str]) -> tuple[str, str]:
    for old, new in zip(before, after):
        if old != new:
            return old.strip()[:70], new.strip()[:70]
    if len(after) > len(before):
        return "", after[len(before)].strip()[:70]
    return before[len(after)].strip()[:70], ""


def source_text(lines: list[str]) -> str:
    return "\n".join(lines)


def accept_code(before: list[str], after: list[str], same: bool) -> bool:
    compared = dumps_same(source_text(before), source_text(after))
    if compared is None:
        return False
    return compared if same else not compared


def generate(kind: str, root: Path, maps: Path, rng: random.Random, count: int) -> list[tuple[str, list[str], list[str]]]:
    """Up to `count` edits: (source, before lines, after lines)."""
    made: list[tuple[str, list[str], list[str]]] = []
    if kind == "comment":
        sites = comment_sites(root)
        for bucket in sites.values():
            rng.shuffle(bucket)
        names = [name for name, bucket in sites.items() if bucket]
        chosen: list[tuple[str, int]] = []
        while len(chosen) < count and any(sites[name] for name in names):
            for name in names:
                if sites[name] and len(chosen) < count:
                    chosen.append(sites[name].pop())
        cache: dict[str, list[str]] = {}
        for source, insert_at in chosen:
            if len(made) >= count:
                break
            if source not in cache:
                cache[source] = read_lines(root / source)
            lines = cache[source]
            indent = ""
            if 1 <= insert_at <= len(lines):
                indent = re.match(r"\s*", lines[insert_at - 1]).group()
            edited = lines[: insert_at - 1] + [f"{indent}# comment"] + lines[insert_at - 1:]
            if accept_code(lines, edited, same=True):
                made.append((source, lines, edited))
        return made

    if kind == "enumcmp":
        pool = enumcmp_candidates(root)
        rng.shuffle(pool)
        cache = {}
        for source, (left, right, old, new) in pool:
            if len(made) >= count:
                break
            if source not in cache:
                cache[source] = read_lines(root / source)
            lines = cache[source]
            start = (left.end_lineno, left.end_col_offset)
            end = (right.lineno, right.col_offset)
            if old not in slice_text(lines, start, end):
                continue
            edited = replace_span(lines, start, end, slice_text(lines, start, end).replace(old, new, 1))
            if accept_code(lines, edited, same=False):
                made.append((source, lines, edited))
        return made

    if kind == "copyflag":
        pool = copyflag_candidates(root)
        rng.shuffle(pool)
        cache = {}
        for source, value in pool:
            if len(made) >= count:
                break
            if source not in cache:
                cache[source] = read_lines(root / source)
            lines = cache[source]
            token = "True" if value.value else "False"
            other = "False" if value.value else "True"
            start = (value.lineno, value.col_offset)
            end = (value.end_lineno, value.end_col_offset)
            if slice_text(lines, start, end) != token:
                continue
            edited = replace_span(lines, start, end, other)
            if accept_code(lines, edited, same=False):
                made.append((source, lines, edited))
        return made

    if kind == "fixture":
        pool = fixture_candidates(root)
        rng.shuffle(pool)
        cache = {}
        for source, line_no in pool:
            if len(made) >= count:
                break
            if source not in cache:
                cache[source] = read_lines(root / source)
            lines = cache[source]
            if line_no < 1 or line_no > len(lines):
                continue
            new = corrupt_token(lines[line_no - 1], rng)
            if new is None:
                continue
            edited = lines[:]
            edited[line_no - 1] = new
            if edited != lines:
                made.append((source, lines, edited))
        return made

    if kind == "random":
        pool = random_candidates(maps)
    elif kind == "classattr":
        pool = classattr_candidates(root)
    elif kind == "deadbranch":
        pool = deadbranch_candidates(root, maps)
    else:
        raise SystemExit(f"mutate: unknown kind {kind}")
    rng.shuffle(pool)
    cache = {}
    seen = set()
    for source, line_no in pool:
        if len(made) >= count:
            break
        if (source, line_no) in seen:
            continue
        seen.add((source, line_no))
        if source not in cache:
            cache[source] = read_lines(root / source)
        lines = cache[source]
        edited = edit_line(lines, line_no, rng)
        if edited is None:
            continue
        if accept_code(lines, edited, same=False):
            made.append((source, lines, edited))
    return made


def main() -> int:
    args = parse_args()
    if args.count < 1:
        print("mutate: count must be at least 1", file=sys.stderr)
        return 1
    rng = random.Random(args.seed)
    args.outdir.mkdir(parents=True, exist_ok=True)
    edits = generate(args.kind, args.sqlglot, args.maps, rng, args.count)
    if len(edits) < args.count:
        print(f"mutate: {args.kind} made {len(edits)} of {args.count}", file=sys.stderr)
        return 1
    for index, (source, before, after) in enumerate(edits):
        mutant = f"r{args.seed}_{args.kind}_{index:02d}"
        write_patch(args.outdir, mutant, source, before, after)
        old, new = preview(before, after)
        print(mutant, source, old, "=>", new)
    return 0


if __name__ == "__main__":
    sys.exit(main())
