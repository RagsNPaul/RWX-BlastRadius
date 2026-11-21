"""The files of a patch -> the tests to run: rules 1-9 of .cursor/rules/10-selector.mdc, in order.

Every answer is worked out from the pinned commit: its sources, and the maps that
tools/make_map.py built from one coverage run of its test suite. Nothing here knows a patch,
or a kind of edit, ahead of time. Where the maps cannot answer, more tests are selected.
"""
import ast
import json
import os

from classify import classify
from patch import apply

_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


class Maps:
    """maps/ and the pinned sources. Each file is loaded once, and only when a patch needs it."""

    def __init__(self, maps_dir, source_dir):
        self.maps_dir, self.source_dir = maps_dir, source_dir
        with open(os.path.join(maps_dir, "index.json")) as f:
            index = json.load(f)
        self.file_union = index["file_union"]  # source -> tests that ran it or use its module
        self.import_lines = index["import_lines"]  # source -> lines that run at import
        self.opens = index["opens"]  # data file -> tests that opened it
        self.py_reads = index["py_reads"]  # source -> tests that read it as text
        self.testfile = index["testfile"]  # test file -> its tests
        self.everything = frozenset(range(len(index["durations"])))
        self._sets = self._blind = self._tests = self._importers = None
        self._lines, self._imported, self._sources = {}, {}, {}

    def check(self, tests):
        """The maps are keyed by test number: refuse a tests.json that numbers tests differently."""
        files = {file: set(numbers) for file, numbers in self.testfile.items()}
        if len(tests) != len(self.everything):
            raise ValueError(f"tests.json has {len(tests)} tests, the maps {len(self.everything)}")
        for test in tests:
            if test["number"] not in files.get(test["file"], ()):
                raise ValueError(f"test {test['number']} is not in {test['file']} in the maps")

    def tests_of(self, index):
        if self._sets is None:
            with open(os.path.join(self.maps_dir, "sets.json")) as f:
                self._sets = json.load(f)
        return self._sets[index]

    def lines(self, path):
        """{"line": set index}: the tests that ran the statement a line belongs to."""
        if path not in self._lines:
            file = os.path.join(self.maps_dir, "lines", path + ".json")
            table = {}
            if os.path.exists(file):
                with open(file) as f:
                    table = json.load(f)
            self._lines[path] = table
        return self._lines[path]

    def imported(self, path):
        if path not in self._imported:
            self._imported[path] = frozenset(self.import_lines.get(path, ()))
        return self._imported[path]

    def source(self, path):
        """(text, tree, ast.dump) of a pinned source, parsed once per run."""
        if path not in self._sources:
            with open(os.path.join(self.source_dir, path), "rb") as f:
                raw = f.read()
            tree = ast.parse(raw)
            self._sources[path] = (raw.decode("utf-8"), tree, ast.dump(tree))
        return self._sources[path]

    def blind(self):
        """Tests that ran no line of sqlglot in the map build (they work in a subprocess, or on
        files outside the package). The line map cannot vouch for them."""
        if self._blind is None:
            seen = set()
            for numbers in self.file_union.values():
                seen.update(numbers)
            self._blind = self.everything - seen
        return self._blind

    def test_sources(self):
        """{path: text} of every Python file under tests/."""
        if self._tests is None:
            self._tests = {}
            for folder, _, names in os.walk(os.path.join(self.source_dir, "tests")):
                for name in names:
                    if name.endswith(".py"):
                        file = os.path.join(folder, name)
                        with open(file, encoding="utf-8", errors="replace") as f:
                            path = os.path.relpath(file, self.source_dir).replace(os.sep, "/")
                            self._tests[path] = f.read()
        return self._tests

    def doctests(self):
        """Tests in files that use doctest (rule 6)."""
        found = set()
        for path, text in self.test_sources().items():
            if "doctest" in text:
                found.update(self.testfile.get(path, ()))
        return found

    def importers(self, path):
        """`path` and every test module that imports it, directly or through another one."""
        if self._importers is None:
            self._importers = {}
            sources = self.test_sources()
            for importer, text in sources.items():
                for name in _imported_names(importer, text):
                    for imported in (name + ".py", name + "/__init__.py"):
                        if imported in sources:
                            self._importers.setdefault(imported, set()).add(importer)
        found, queue = {path}, [path]
        while queue:
            for importer in self._importers.get(queue.pop(), ()):
                if importer not in found:
                    found.add(importer)
                    queue.append(importer)
        return found


def _imported_names(path, text):
    """Module paths (no .py) that the import statements of a test module may name."""
    package, lines, found = path.split("/")[:-1], text.split("\n"), set()
    for i, line in enumerate(lines):
        words = line.split()
        if len(words) < 2 or words[0] not in ("from", "import"):
            continue
        if words[0] == "import":
            module, names = [], line.split("import", 1)[1]
        else:
            names = line.partition(" import ")[2]
            dotted = words[1].lstrip(".")
            level = len(words[1]) - len(dotted)
            module = package[: len(package) - level + 1] if level else []
            module = module + (dotted.split(".") if dotted else [])
            found.add("/".join(module))
            while "(" in names and ")" not in names and i + 1 < len(lines):
                i += 1
                names += lines[i]
        for name in names.replace("(", " ").replace(")", " ").split(","):
            if name.split():
                found.add("/".join(module + name.split()[0].split(".")))
    return found


# --- units: the statement a line belongs to -------------------------------------------------
def _start(node):
    return min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", ())])


def _unit(tree, line):
    """(first line, last line, node) of the unit that owns `line`, or None for a line that is
    blank, a comment or a bare `else:`.

    Units are those of tools/make_map.py: a simple statement, the header of a compound statement
    (decorators included, up to the line before its body), or a lambda in import-time code. The
    innermost one wins. The map gives every line of a unit the same tests.
    """
    found, depth_found, stack = None, -1, [(tree, False, 0)]
    while stack:
        node, in_function, depth = stack.pop()
        for child in ast.iter_child_nodes(node):
            if hasattr(child, "lineno"):
                first = _start(child)
                if not first <= line <= child.end_lineno:
                    continue
                is_statement = isinstance(child, (ast.stmt, ast.ExceptHandler))
                if is_statement or (isinstance(child, ast.Lambda) and not in_function):
                    body = getattr(child, "body", None) if is_statement else None
                    last = max(first, _start(body[0]) - 1) if body else child.end_lineno
                    if line <= last and depth > depth_found:
                        found, depth_found = (first, last, child), depth
            stack.append((child, in_function or isinstance(child, _FUNCTIONS), depth + 1))
    return found


def _within_lambda_body(node, line, old, new):
    """Does a change to `line` of an import-time lambda stay inside the lambda's body?

    Then only the tests that called the lambda can see it. Otherwise the statement around the
    lambda (a dict key, a default, a call argument) may have changed too.
    """
    if line > node.lineno:
        return True
    if new is None or node.body.lineno != line:
        return False
    old, new = old.encode("utf-8"), new.encode("utf-8")  # the ast counts columns in bytes
    shortest = min(len(old), len(new))
    prefix = 0
    while prefix < shortest and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < shortest - prefix and old[-1 - suffix] == new[-1 - suffix]:
        suffix += 1
    after = len(old) - node.end_col_offset if node.end_lineno == line else 0
    return prefix >= node.body.col_offset and suffix >= after


# --- rule 2: the old lines a patch changes --------------------------------------------------
def _indent(rows):
    return min((len(row) - len(row.lstrip()) for row in rows if row.strip()), default=None)


def _changed(hunks, unit, line_count):
    """{old line: its new text, or None when there is no one-to-one replacement}.

    Removed lines count by their number. Where nothing that runs was removed (a pure insertion,
    or a removed comment, blank line or `else:`), the nearest statements on both sides stand in:
    the tests that reach them are the ones that can reach the new lines. The same is done when
    the new lines are indented less than the removed ones, since they may run where those did not.
    """
    replaced, around = {}, set()

    def nearest(line, step):
        while 0 < line <= line_count:
            if unit(line):
                around.add(line)
                return
            line += step

    for rows in hunks:
        i = 0
        while i < len(rows):
            if rows[i][0] == " ":
                i += 1
                continue
            start = i
            while i < len(rows) and rows[i][0] != " ":
                i += 1
            block = rows[start:i]
            removed = [(number, row) for kind, number, row in block if kind == "-"]
            added = [row for kind, _, row in block if kind == "+"]
            if not removed:
                nearest(block[0][1] - 1, -1)
                nearest(block[0][1], 1)
                continue
            for position, (number, _) in enumerate(removed):
                if unit(number):
                    paired = added[position] if len(added) == len(removed) else None
                    replaced.setdefault(number, paired)
                else:
                    nearest(number - 1, -1)
                    nearest(number + 1, 1)
            new_indent, old_indent = _indent(added), _indent([row for _, row in removed])
            if new_indent is not None and old_indent is not None and new_indent < old_indent:
                nearest(removed[0][0] - 1, -1)
                nearest(removed[-1][0] + 1, 1)
    replaced.update(dict.fromkeys(around))
    return replaced


# --- rules 1-6: sqlglot/**/*.py -------------------------------------------------------------
def _source(path, hunks, maps):
    tests = set(maps.py_reads.get(path, ()))  # rule 7 also holds for a source that tests read as text
    text, tree, dump = maps.source(path)
    try:
        # Parsed from bytes, as the import system does, so a broken encoding line is an error here too
        same = ast.dump(ast.parse(apply(text, hunks).encode("utf-8"))) == dump
    except (SyntaxError, ValueError, UnicodeError) as error:
        return maps.everything, f"all:{type(error).__name__}"  # the module no longer imports
    if same:
        return tests, "noop"  # rule 1: only comments or layout changed

    units = {}

    def unit(line):
        if line not in units:
            units[line] = _unit(tree, line)
        return units[line]

    old_lines = text.split("\n")
    table, imported = maps.lines(path), maps.imported(path)
    indexes, at_import, docstring = set(), False, False
    for line, new in _changed(hunks, unit, len(old_lines)).items():  # rule 2
        first, last, node = unit(line)
        if isinstance(node, _DEFINITIONS):
            last = node.end_lineno  # a def, class or decorator line: everything in its body
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            docstring = docstring or isinstance(node.value.value, str)
        inside = isinstance(node, ast.Lambda) and _within_lambda_body(node, line, old_lines[line - 1], new)
        for number in range(first, last + 1):
            index = table.get(str(number))
            if index is not None:
                indexes.add(index)  # rule 3: tests that ran the statement
            if not inside and number in imported:
                at_import = True  # rule 4: it also runs when the module is imported
    for index in indexes:
        tests.update(maps.tests_of(index))
    reason = "line" if indexes else "uncovered"  # rule 5: a line that never ran selects nothing
    if at_import:
        if path not in maps.file_union:
            return maps.everything, "all:import-unmapped"  # imported, yet no test is known to use it
        tests.update(maps.file_union[path])
        tests.update(maps.blind())
        reason = "import->file"
    if docstring:
        tests.update(maps.doctests())  # rule 6
        reason += "+docstring"
    return tests, reason


def _new_source(file, files, maps):
    """A new module under sqlglot/: nothing at the pinned commit imports it, so it only matters
    when the same patch starts to use it, or when a test reads every module of its folder."""
    name = os.path.basename(file.new)[:-3]
    if name == "__init__" or os.path.exists(os.path.join(maps.source_dir, file.new)):
        return maps.everything, "all:new-package"
    for other in files:
        if other is not file and any(kind == "+" and name in row for rows in other.hunks for kind, _, row in rows):
            return maps.everything, "all:new-imported"
    folder, tests = os.path.dirname(file.new), set()
    for path, numbers in maps.py_reads.items():
        if os.path.dirname(path) == folder:
            tests.update(numbers)
    return tests, "new"


# --- rule 7: fixtures and other data files --------------------------------------------------
def _data(path, maps):
    if path in maps.opens:
        return set(maps.opens[path])
    # No test opened it by name (a new file, or one found by listing a folder): take the tests
    # that opened its nearest neighbours.
    folder = os.path.dirname(path)
    while folder and folder != "tests":
        tests = set()
        for other, numbers in maps.opens.items():
            if other.startswith(folder + "/"):
                tests.update(numbers)
        if tests:
            return tests
        folder = os.path.dirname(folder)
    return maps.everything


# --- rule 8: tests/**/*.py ------------------------------------------------------------------
def _test(path, maps):
    if path not in maps.testfile:
        return maps.everything, "all:test-support"  # helpers, conftest, __init__: any test may use it
    tests = set()
    for file in maps.importers(path):
        if file not in maps.testfile:
            return maps.everything, "all:test-support"
        tests.update(maps.testfile[file])
    return tests, "testfile"


def _file(file, files, maps):
    paths = {path for path in (file.old, file.new) if path}
    kinds = {classify(path, maps.opens) for path in paths}
    if kinds == {"data"}:  # changed, added, deleted or renamed: every name it had or gets
        return set().union(*(_data(path, maps) for path in paths)), "data"
    if file.old is None and kinds == {"source"}:
        return _new_source(file, files, maps)
    if file.old != file.new or file.binary or not file.hunks:
        return maps.everything, "all:" + ("added-deleted-renamed" if file.old != file.new else "no-text-change")
    if kinds == {"source"}:
        return _source(file.old, file.hunks, maps)
    if kinds == {"test"}:
        return _test(file.old, maps)
    return maps.everything, "all:other"  # rule 9


def select(files, maps):
    """-> (test numbers, [reason per file]): the union over the files of one patch."""
    selected, reasons = set(), []
    for file in files:
        tests, reason = _file(file, files, maps)
        selected.update(tests)
        reasons.append(reason)
        if len(selected) == len(maps.everything):
            break
    return selected, reasons
