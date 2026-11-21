"""Build maps/words.json from the pinned commit.

A word maps to the tests whose own source, or a fixture file they opened, contains
that word. Matching is case-insensitive. The selector only reads the finished index.
"""
import json
import os
import re
import sys

WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")  # at least two characters


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root = os.path.join(repo, "sqlglot")
    maps = os.path.join(repo, "maps")
    with open(os.path.join(maps, "index.json")) as f:
        index = json.load(f)
    words = {}

    def add(path, tests):
        if not tests or path.endswith(".gz"):
            return
        full = os.path.join(root, path)
        if not os.path.isfile(full):
            return
        with open(full, encoding="utf-8", errors="replace") as f:
            text = f.read()
        found = {match.group(0).casefold() for match in WORD.finditer(text)}
        for word in found:
            words.setdefault(word, set()).update(tests)

    for path, tests in index["testfile"].items():
        add(path, tests)
    for path, tests in index["opens"].items():
        if path.startswith("tests/fixtures/"):
            add(path, tests)

    out = {word: sorted(numbers) for word, numbers in sorted(words.items())}
    dest = os.path.join(maps, "words.json")
    with open(dest, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"{len(out)} words -> {dest}")


if __name__ == "__main__":
    sys.exit(main())
