#!/usr/bin/env python3
"""Write N mutant patches into bank/. Checking only: bank/ never ships in the image.

    python3 tools/mutate.py -n 20 --seed 1
    python3 tools/mutate.py -n 15 --seed 2 --focus import

Kinds follow .cursor/rules/30-eval-loop.mdc (code 55%, fixture 25%, comment 10%, multi 10%).
Each id is m0001, m0002, ... one past the largest id already in bank/ledger.jsonl, so an id
is never reused. The same seed and id always try the same edits.
"""
import argparse
import ast
import difflib
import glob
import io
import json
import os
import random
import re
import subprocess
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQ = os.path.join(ROOT, "sqlglot")
BANK = os.path.join(ROOT, "bank")
LEDGER = os.path.join(BANK, "ledger.jsonl")
KINDS = ["code"] * 55 + ["fixture"] * 25 + ["comment"] * 10 + ["multi"] * 10
SUBKINDS = ["code"] * 55 + ["fixture"] * 25 + ["comment"] * 10
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
INT = re.compile(r"-?\d(?:_?\d)*\Z")


def log(*message):
    print("mutate:", *message, file=sys.stderr)


def rel(path):
    return os.path.relpath(path, SQ).replace(os.sep, "/")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def offsets(src):
    starts, position = [], 0
    for line in src.split("\n"):
        starts.append(position)
        position += len(line) + 1
    return starts


def span(starts, tok):
    return starts[tok.start[0] - 1] + tok.start[1], starts[tok.end[0] - 1] + tok.end[1]


def name(tok, text):
    return tok.type == tokenize.NAME and tok.string == text


def op(tok, text):
    return tok.type == tokenize.OP and tok.string == text


def tweak_string(text):
    quote = min((i for i, char in enumerate(text) if char in "\"'"), default=None)
    if quote is None:
        return None
    chars = list(text)
    for i in range(quote + 1, len(chars) - 1):
        if chars[i].isalpha() and chars[i - 1] != "\\":
            chars[i] = chars[i].swapcase()
            return "".join(chars)
    return None


def collect_code(path, src):
    """Single-line edits. Comments and strings are never picked by the operator edits."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return [], set()
    starts = offsets(src)
    sites, lambdas = [], set()
    for index, tok in enumerate(tokens):
        if name(tok, "lambda"):
            lambdas.add(tok.start[0])
        start, end = span(starts, tok)
        add = sites.append
        if name(tok, "and"):
            add((tok.start[0], "and->or", start, end, "or"))
        elif name(tok, "or"):
            add((tok.start[0], "or->and", start, end, "and"))
        elif op(tok, "=="):
            add((tok.start[0], "==->!=", start, end, "!="))
        elif op(tok, "!="):
            add((tok.start[0], "!=->==", start, end, "=="))
        elif op(tok, "<"):
            add((tok.start[0], "<-><=", start, end, "<="))
        elif op(tok, "<="):
            add((tok.start[0], "<=-<", start, end, "<"))
        elif op(tok, "+"):
            add((tok.start[0], "+->-", start, end, "-"))
        elif op(tok, "-"):
            add((tok.start[0], "-->+", start, end, "+"))
        elif name(tok, "True"):
            add((tok.start[0], "True->False", start, end, "False"))
        elif name(tok, "False"):
            add((tok.start[0], "False->True", start, end, "True"))
        elif name(tok, "is") and not (index + 1 < len(tokens) and name(tokens[index + 1], "not")):
            add((tok.start[0], "is->==", start, end, "=="))
        elif name(tok, "not") and not (index > 0 and name(tokens[index - 1], "is")):
            add((tok.start[0], "not-removed", start, end, ""))
        elif tok.type == tokenize.NUMBER and INT.fullmatch(tok.string):
            value = int(tok.string)
            add((tok.start[0], "int+1", start, end, str(value + 1)))
            add((tok.start[0], "int-1", start, end, str(value - 1)))
        elif tok.type == tokenize.STRING and tok.start[0] == tok.end[0]:
            tweaked = tweak_string(tok.string)
            if tweaked and tweaked != tok.string:
                add((tok.start[0], "string", start, end, tweaked))
        if (
            name(tok, "seq_get")
            and index + 5 < len(tokens)
            and op(tokens[index + 1], "(")
            and name(tokens[index + 2], "args")
            and op(tokens[index + 3], ",")
            and tokens[index + 4].type == tokenize.NUMBER
            and tokens[index + 4].string == "0"
            and op(tokens[index + 5], ")")
        ):
            number = tokens[index + 4]
            add((number.start[0], "seq_get-0->1", *span(starts, number), "1"))
        if name(tok, "copy") and index + 2 < len(tokens) and op(tokens[index + 1], "=") and name(tokens[index + 2], "False"):
            flag = tokens[index + 2]
            add((flag.start[0], "copy=False->True", *span(starts, flag), "True"))
    return sites, lambdas


def comment_sites(src):
    lines = src.splitlines()
    sites = [0]
    for number, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if stripped.startswith(("def ", "async def ", "class ", '"""', "'''")):
            sites.append(number)
    return sites


def insert_comment(src, before):
    lines = src.splitlines(True)
    if before == 0:
        idx = 1 if lines and lines[0].lstrip().startswith("#") and "coding" in lines[0] else 0
    else:
        idx = before - 1
    lines.insert(idx, "# Kept for backwards compatibility\n")
    return "".join(lines)


def fixture_files():
    found = []
    for path in glob.glob(os.path.join(SQ, "tests", "fixtures", "**", "*"), recursive=True):
        if not os.path.isfile(path) or path.endswith((".gz", ".png", ".jpg", ".pyc")):
            continue
        try:
            text = read(path)
        except UnicodeError:
            continue
        lines = []
        for number, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith(("#", "--")) or not WORD.search(line):
                continue
            lines.append(number)
        if lines:
            found.append((rel(path), lines))
    return found


def corrupt(line):
    match = WORD.search(line)
    if not match:
        return None
    word = match.group(0)
    replacement = word[:-1] + ("1" if word[-1] != "1" else "2")
    return line[: match.start()] + replacement + line[match.end() :]


def apply_line(src, number, new_line):
    lines = src.splitlines(True)
    old = lines[number - 1]
    ending = "\n" if old.endswith("\n") else ""
    lines[number - 1] = new_line.rstrip("\n") + ending
    return "".join(lines)


def udiff(path, old, new):
    body = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{path}", f"b/{path}"))
    if not body:
        return ""
    return f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n{body}"


def dumps_equal(old, new):
    return ast.dump(ast.parse(old)) == ast.dump(ast.parse(new))


def python_ok(old, new, equal):
    try:
        return dumps_equal(old, new) if equal else not dumps_equal(old, new)
    except SyntaxError:
        return False


def git_ok(diff):
    run = subprocess.run(
        ["git", "-C", SQ, "apply", "--check", "--whitespace=nowarn", "-"],
        input=diff, text=True, capture_output=True,
    )
    return run.returncode == 0


class Corpus:
    def __init__(self, focus):
        self.focus = focus
        self.code, self.comments, self.lambdas = {}, {}, {}
        self.text = {}
        for path in sorted(glob.glob(os.path.join(SQ, "sqlglot", "**", "*.py"), recursive=True)):
            key, src = rel(path), read(path)
            sites, lambdas = collect_code(path, src)
            self.text[key] = src
            self.code[key] = sites
            self.comments[key] = comment_sites(src)
            self.lambdas[key] = lambdas
        self.fixtures = fixture_files()
        for key, _ in self.fixtures:
            self.text[key] = read(os.path.join(SQ, key))
        self.import_lines, self.covered = {}, {}
        if focus in ("import", "covered", "uncovered", "lambda"):
            with open(os.path.join(ROOT, "maps", "index.json")) as f:
                index = json.load(f)
            self.import_lines = {path: set(lines) for path, lines in index["import_lines"].items()}
            for file in glob.glob(os.path.join(ROOT, "maps", "lines", "**", "*.json"), recursive=True):
                path = os.path.relpath(file, os.path.join(ROOT, "maps", "lines"))[: -len(".json")]
                with open(file) as f:
                    self.covered[path] = {int(line) for line in json.load(f)}

    def wanted(self, path, line):
        focus = self.focus
        if focus is None:
            return True
        if focus == "lambda":
            return line in self.lambdas.get(path, ())
        imported = line in self.import_lines.get(path, ())
        covered = line in self.covered.get(path, ())
        if focus == "import":
            return imported
        if focus == "covered":
            return covered
        if focus == "uncovered":
            return not imported and not covered
        return True


def load_ledger():
    rows, used, highest = [], set(), 0
    if os.path.exists(LEDGER):
        with open(LEDGER) as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                rows.append(row)
                used.add((row["file"], row["line"], row["operator"]))
                highest = max(highest, int(row["id"][1:]))
    taken = {row["id"] for row in rows}
    taken.update(os.path.splitext(name)[0] for name in os.listdir(BANK) if name.endswith(".patch")) if os.path.isdir(BANK) else None
    return used, highest, taken


def next_ids(count, highest, taken):
    ids, number = [], highest
    while len(ids) < count:
        number += 1
        ident = f"m{number:04d}"
        if ident not in taken:
            ids.append((number, ident))
    return ids


def choose_code(corpus, rng, avoid, banned):
    pool = [
        (path, site)
        for path, sites in corpus.code.items()
        if path not in avoid
        for site in sites
        if corpus.wanted(path, site[0]) and (path, site[0], site[1]) not in banned
    ]
    if not pool:
        return None
    path, (line, operator, start, end, replacement) = rng.choice(pool)
    old = corpus.text[path]
    new = old[:start] + replacement + old[end:]
    if not python_ok(old, new, equal=False):
        banned.add((path, line, operator))
        return None
    return path, line, operator, old, new


def choose_comment(corpus, rng, avoid, banned):
    pool = [
        (path, line)
        for path, lines in corpus.comments.items()
        if path not in avoid
        for line in lines
        if (path, line, "comment") not in banned
    ]
    if not pool:
        return None
    path, line = rng.choice(pool)
    old = corpus.text[path]
    new = insert_comment(old, line)
    if not python_ok(old, new, equal=True):
        banned.add((path, line, "comment"))
        return None
    return path, line, "comment", old, new


def choose_fixture(corpus, rng, avoid, banned):
    pool = [
        (path, line)
        for path, lines in corpus.fixtures
        if path not in avoid
        for line in lines
        if (path, line, "fixture") not in banned
    ]
    if not pool:
        return None
    path, line = rng.choice(pool)
    old = corpus.text[path]
    lines = old.splitlines()
    changed = corrupt(lines[line - 1])
    if changed is None or changed == lines[line - 1]:
        banned.add((path, line, "fixture"))
        return None
    return path, line, "fixture", old, apply_line(old, line, changed)


def one_edit(corpus, rng, kind, avoid, banned):
    for _ in range(60):
        if kind == "code":
            edit = choose_code(corpus, rng, avoid, banned)
        elif kind == "comment":
            edit = choose_comment(corpus, rng, avoid, banned)
        else:
            edit = choose_fixture(corpus, rng, avoid, banned)
        if edit:
            return edit
    return None


def changed_line(operator, old, new, line):
    if operator == "comment":
        return "", "# Kept for backwards compatibility"
    old_lines, new_lines = old.splitlines(), new.splitlines()
    if line - 1 < len(old_lines) and line - 1 < len(new_lines) and old_lines[line - 1] != new_lines[line - 1]:
        return old_lines[line - 1], new_lines[line - 1]
    for before, after in zip(old_lines, new_lines):
        if before != after:
            return before, after
    return "", ""


def build(corpus, rng, kind, banned):
    if kind == "fixture" or corpus.focus == "fixture":
        kind = "fixture"
    parts = rng.choice([2, 3]) if kind == "multi" else 1
    edits, files = [], set()
    for _ in range(parts):
        sub = rng.choice(SUBKINDS) if kind == "multi" else kind
        edit = one_edit(corpus, rng, sub, files, banned)
        if edit is None:
            return None
        edits.append(edit)
        files.add(edit[0])
    diff = "".join(udiff(path, old, new) for path, _, _, old, new in edits)
    if not diff or not git_ok(diff):
        for path, line, operator, _, _ in edits:
            banned.add((path, line, operator))
        return None
    return kind, edits, diff


def main():
    parser = argparse.ArgumentParser(description="Write mutant patches into bank/.")
    parser.add_argument("-n", type=int, required=True)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--focus", choices=("import", "covered", "uncovered", "lambda", "fixture"))
    args = parser.parse_args()
    if args.n < 1:
        log("-n must be at least 1")
        return 1
    os.makedirs(BANK, exist_ok=True)
    corpus = Corpus(args.focus)
    banned, highest, taken = load_ledger()
    written = []
    with open(LEDGER, "a") as ledger:
        for number, ident in next_ids(args.n, highest, taken):
            rng = random.Random(args.seed * 100000 + number)
            kind = "fixture" if args.focus == "fixture" else rng.choice(KINDS)
            made = None
            for _ in range(40):
                made = build(corpus, rng, kind, banned)
                if made:
                    break
            if made is None:
                log(f"gave up on {ident} ({kind})")
                return 1
            kind, edits, diff = made
            with open(os.path.join(BANK, ident + ".patch"), "w") as f:
                f.write(diff)
            for path, line, operator, old, new in edits:
                before, after = changed_line(operator, old, new, line)
                row = {
                    "id": ident, "seed": args.seed, "kind": kind, "operator": operator,
                    "file": path, "line": line, "before": before, "after": after,
                }
                if args.focus:
                    row["focus"] = args.focus
                ledger.write(json.dumps(row, separators=(",", ":")) + "\n")
                banned.add((path, line, operator))
            written.append((ident, kind, [operator for _, _, operator, _, _ in edits]))
            log(ident, kind, ",".join(f"{operator}@{path}:{line}" for path, line, operator, _, _ in edits))
    log(f"wrote {len(written)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
