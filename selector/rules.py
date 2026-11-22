"""The files of a patch -> the tests to run: rules 1-9 of .cursor/rules/10-selector.mdc, in order.

Every answer is worked out from the pinned commit: its sources, and the maps that
tools/make_map.py built from one coverage run of its test suite. Nothing here knows a patch,
or a kind of edit, ahead of time. Where the maps cannot answer, more tests are selected.
"""
import ast
import copy
import io
import json
import math
import os
import re
import resource
import shutil
import subprocess
import sys
import tokenize

from classify import classify
from patch import apply

_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")


def _narrow_class():
    """Class bodies with an inst entry skip file_union. NARROW_CLASS=0 restores it."""
    return os.environ.get("NARROW_CLASS", "1") == "1"


def _equiv_enum():
    """is/== swaps on a plain Enum attribute are not a change. EQUIV_ENUM=0 keeps them."""
    return os.environ.get("EQUIV_ENUM", "1") == "1"


def _equiv_dead():
    """A change that stays inside a body no test runs is not a change. EQUIV_DEAD=0 keeps it."""
    return os.environ.get("EQUIV_DEAD", "1") == "1"


def _import_check():
    """Fingerprint the patched import before taking every test. IMPORT_CHECK=0 keeps the fallback."""
    return os.environ.get("IMPORT_CHECK", "1") == "1"


def _expensive_check():
    """Drop a selected test over 3s when a cheap check shows the patch cannot change it."""
    return os.environ.get("EXPENSIVE_CHECK", "0") == "1"


def _word_no_expensive():
    """The word rule may not add a test that takes over 1s. WORD_NO_EXPENSIVE=0 keeps them."""
    return os.environ.get("WORD_NO_EXPENSIVE", "0") == "1"


def _word_off():
    """The word rule searches only string literals the patch removes and does not write back."""
    return os.environ.get("WORD_OFF", "0") == "1"


def _class_cap():
    """A class used by more than 400 tests keeps its file's tests after the import check."""
    return os.environ.get("CLASS_CAP", "0") == "1"


def _neighbor_off():
    """A pure insertion maps to the statement it lands in, not the statements on both sides."""
    return os.environ.get("NEIGHBOR_OFF", "0") == "1"


def _dialect_file_off():
    """Line coverage of a dialect does not also add that dialect's whole test file."""
    return os.environ.get("DIALECT_FILE_OFF", "0") == "1"


def _slow_policy():
    """Drop the slow parser, generator, and TPC tests last. SLOW_POLICY=0 keeps them."""
    return os.environ.get("SLOW_POLICY", "1") == "1"


_SLOW_MS = 3000
_WORD_SLOW_MS = 1000
_CLASS_CAP = 400


_ENUM_SWAP = {
    (ast.Is, ast.Eq),
    (ast.Eq, ast.Is),
    (ast.IsNot, ast.NotEq),
    (ast.NotEq, ast.IsNot),
}


class Maps:
    """maps/ and the pinned sources. Each file is loaded once, and only when a patch needs it."""

    def __init__(self, maps_dir, source_dir):
        self.maps_dir, self.source_dir = maps_dir, source_dir
        with open(os.path.join(maps_dir, "index.json")) as f:
            index = json.load(f)
        self.file_union = index["file_union"]  # source -> tests that ran it or use its module
        self.durations = index["durations"]  # milliseconds, indexed by test number
        self.import_lines = index["import_lines"]  # source -> lines that run at import
        self.opens = index["opens"]  # data file -> tests that opened it
        self.py_reads = index["py_reads"]  # source -> tests that read it as text
        self.testfile = index["testfile"]  # test file -> its tests
        self.everything = frozenset(range(len(index["durations"])))
        self.import_classes = set()  # module:QualName created while importing sqlglot
        self.inst = {}  # module:QualName -> tests that instantiated it or a subclass
        self._sets = self._blind = self._tests = self._importers = self._words = self._enums = None
        self._scalars = {}
        self._lines, self._imported, self._sources = {}, {}, {}
        self._import_fp = self._tpc = self._max_lines = self._risk = None
        self._line_counts = self._test_path = None
        self.by_id = {}

    def check(self, tests):
        """The maps are keyed by test number: refuse a tests.json that numbers tests differently."""
        files = {file: set(numbers) for file, numbers in self.testfile.items()}
        if len(tests) != len(self.everything):
            raise ValueError(f"tests.json has {len(tests)} tests, the maps {len(self.everything)}")
        for test in tests:
            if test["number"] not in files.get(test["file"], ()):
                raise ValueError(f"test {test['number']} is not in {test['file']} in the maps")
        self.by_id = {test["id"]: test["number"] for test in tests}
        self._load_classes(tests)

    def tpc(self):
        """Pinned TPC-H / TPC-DS optimize().sql() hashes and the files each query calls."""
        if self._tpc is None:
            with open(os.path.join(self.maps_dir, "tpc.json"), encoding="utf-8") as handle:
                self._tpc = json.load(handle)
        return self._tpc

    def max_node_lines(self):
        """Lines test_max_nodes runs that also sit on the default dialect's parse path."""
        if self._max_lines is None:
            with open(os.path.join(self.maps_dir, "max_nodes_lines.json"), encoding="utf-8") as handle:
                self._max_lines = set(json.load(handle))
        return self._max_lines

    def line_counts(self):
        """How many mapped lines each test ran. Built once from the pinned line maps."""
        if self._line_counts is None:
            counts = [0] * len(self.durations)
            root = os.path.join(self.maps_dir, "lines")
            for dirpath, _dirs, names in os.walk(root):
                for name in names:
                    if not name.endswith(".json"):
                        continue
                    with open(os.path.join(dirpath, name)) as handle:
                        table = json.load(handle)
                    for index in table.values():
                        for number in self.tests_of(index):
                            counts[number] += 1
            self._line_counts = counts
        return self._line_counts

    def test_path(self, number):
        if self._test_path is None:
            found = {}
            for path, numbers in self.testfile.items():
                for test in numbers:
                    found.setdefault(test, path)
            self._test_path = found
        return self._test_path.get(number, "")

    def risk_weights(self):
        """Eight logistic weights: intercept plus the seven risk features."""
        if self._risk is None:
            with open(os.path.join(self.maps_dir, "risk.json"), encoding="utf-8") as handle:
                self._risk = [float(value) for value in json.load(handle)]
        return self._risk

    def tests_of(self, index):
        if self._sets is None:
            with open(os.path.join(self.maps_dir, "sets.json")) as f:
                self._sets = json.load(f)
        return self._sets[index]

    def _load_classes(self, tests):
        """Instantiation map. inst.json lists, per test, every sqlglot class in that object's MRO."""
        by_id = {test["id"]: test["number"] for test in tests}
        with open(os.path.join(self.maps_dir, "import_classes.json")) as f:
            self.import_classes = set(json.load(f))
        with open(os.path.join(self.maps_dir, "inst.json")) as f:
            recorded = json.load(f)
        inst = {}
        for nodeid, names in recorded.items():
            key = "::".join(nodeid.split("[", 1)[0].split("::")[:3])
            number = by_id.get(key)
            if number is None:
                continue
            for name in names:
                inst.setdefault(name, set()).add(number)
        self.inst = inst

    def words_of(self, words):
        """Tests whose source or an opened fixture contains any of these casefolded words."""
        if self._words is None:
            file = os.path.join(self.maps_dir, "words.json")
            with open(file) as f:
                self._words = json.load(f)
        found = set()
        for word in words:
            found.update(self._words.get(word, ()))
        if _word_no_expensive():
            found = {number for number in found if self.durations[number] <= _WORD_SLOW_MS}
        return found

    def _load_enums(self):
        if self._enums is None:
            with open(os.path.join(self.maps_dir, "enums.json")) as f:
                recorded = json.load(f)
            self._enums = {file: frozenset(names) for file, names in recorded["names"].items()}
            self._scalars = {file: frozenset(names) for file, names in recorded.get("scalars", {}).items()}

    def enum_names(self, path):
        """Local names of Enum classes whose equality is still identity. Loaded once."""
        self._load_enums()
        return self._enums.get(path, frozenset())

    def scalar_names(self, path):
        """Local names of Token and TokenType. `== None` matches `is None` for these."""
        self._load_enums()
        return self._scalars.get(path, frozenset())

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
def _class_chain(tree, line):
    """Qualname of the class body that contains `line`, when the line is not inside a function.

    The same walk as prototype/reference_selector.py variant B: a class-level statement names
    its class, and a line inside a method names nothing.
    """
    best = None

    def walk(node, prefix):
        nonlocal best
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef) and child.lineno <= line <= child.end_lineno:
                qualname = prefix + [child.name]
                best = qualname
                walk(child, qualname)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.lineno <= line <= child.end_lineno:
                if best and len(best) == len(prefix):
                    best = None
                return

    walk(tree, [])
    return ".".join(best) if best else None


def _in_function(tree, line):
    """True when `line` sits inside a function or method, including one nested in a statement."""
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if not hasattr(child, "lineno") or not child.lineno <= line <= child.end_lineno:
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return True
            stack.append(child)
    return False


def _module_name(path):
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[:-9]
    return module


def _class_union(key, maps):
    """file_union of the module that defines `module:QualName`, when the map has it."""
    module = key.partition(":")[0]
    base = module.replace(".", "/")
    for path in (base + ".py", base + "/__init__.py"):
        if path in maps.file_union:
            return maps.file_union[path]
    return None


def _dialect_tests(path, maps):
    """Tests in tests/dialects/test_<name>.py for a sqlglot/dialects/<name>.py edit."""
    if not path.startswith("sqlglot/dialects/") or not path.endswith(".py"):
        return set()
    name = os.path.basename(path)[:-3]
    if name == "__init__":
        return set()
    return set(maps.testfile.get(f"tests/dialects/test_{name}.py", ()))


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
    replaced, around, neighbors = {}, set(), set()

    def nearest(line, step):
        while 0 < line <= line_count:
            if unit(line):
                around.add(line)
                return line
            line += step
        return None

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
                if _neighbor_off():
                    nearest(block[0][1], 1)  # the statement the new lines are inserted into
                else:
                    back = nearest(block[0][1] - 1, -1)
                    nearest(block[0][1], 1)
                    if back is not None:
                        neighbors.add(back)
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
    return replaced, neighbors


def _string_literals(text):
    """String values written on one diff line. A broken line contributes nothing rather than a guess."""
    found = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.STRING:
                try:
                    value = ast.literal_eval(token.string)
                except (SyntaxError, ValueError):
                    continue
                if isinstance(value, str) and value:
                    found.add(value)
            elif token.type == tokenize.FSTRING_MIDDLE and token.string:
                found.add(token.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return set()
    return found


def _words_of(literals):
    """Casefolded words inside changed string literals. A literal with none cannot be searched."""
    words, searchable = set(), True
    for literal in literals:
        found = {match.group(0).casefold() for match in _WORD.finditer(literal)}
        if not found:
            searchable = False
        words |= found
    return words, searchable


def _places(tree):
    """One walk: lines inside any function, and class-body lines keyed by QualName.

    A function wins over the class that contains it, including a function nested in a statement,
    which is the order `_source` uses.
    """
    functions, classes = set(), {}

    def walk(node, prefix, in_function):
        for child in ast.iter_child_nodes(node):
            if not hasattr(child, "lineno"):
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.update(range(child.lineno, child.end_lineno + 1))
                walk(child, prefix, True)
            elif isinstance(child, ast.ClassDef):
                qualname = prefix + [child.name]
                if not in_function:
                    name = ".".join(qualname)
                    for line in range(child.lineno, child.end_lineno + 1):
                        classes[line] = name
                walk(child, qualname, in_function)
            else:
                walk(child, prefix, in_function)

    walk(tree, [], False)
    return functions, classes


def _class_edits(hunks, tree, path):
    """{module:QualName: (changed words, searchable)} for class-body lines of this patch.

    `searchable` is false when a changed literal has no word to look up, or the hunk touches
    more than one class. Those classes keep file_union.
    """
    functions, classes = _places(tree)
    module = _module_name(path)

    def key_at(line):
        if line in functions:
            return None
        name = classes.get(line)
        return f"{module}:{name}" if name else None

    pending = {}
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
            keys = {key for number, _ in removed if (key := key_at(number))}
            if not removed:
                inserted = block[0][1]
                keys = {key for line in (inserted - 1, inserted) if line > 0 and (key := key_at(line))}
            if len(keys) != 1:
                for key in keys:
                    pending[key] = None
                continue
            key = next(iter(keys))
            if key in pending and pending[key] is None:
                continue
            slot = pending.setdefault(key, [set(), set()])
            for _, row in removed:
                slot[0] |= _string_literals(row)
            for row in added:
                slot[1] |= _string_literals(row)
    edits = {}
    for key, slot in pending.items():
        if slot is None:
            edits[key] = None
            continue
        # WORD_OFF keeps a literal only when the patch removes it and does not write it back.
        literals = (slot[0] - slot[1]) if _word_off() else slot[0] ^ slot[1]
        words, searchable = _words_of(literals)
        edits[key] = (words, searchable)
    return edits


def _line_map(hunks, line_count):
    """Old line -> new line for context lines and one-for-one replacements."""
    mapping, new_line, position = {}, 1, 1
    for rows in hunks:
        if not rows:
            continue
        first = rows[0][1]
        while position < first:
            mapping[position] = new_line
            position += 1
            new_line += 1
        index = 0
        while index < len(rows):
            kind, number, _ = rows[index]
            if kind == " ":
                mapping[number] = new_line
                new_line += 1
                position = number + 1
                index += 1
                continue
            start = index
            while index < len(rows) and rows[index][0] != " ":
                index += 1
            block = rows[start:index]
            removed = [num for kind, num, _ in block if kind == "-"]
            added = [row for kind, _, row in block if kind == "+"]
            if removed and len(removed) == len(added):
                for num in removed:
                    mapping[num] = new_line
                    new_line += 1
                    position = num + 1
            else:
                new_line += len(added)
                if removed:
                    position = removed[-1] + 1
    while position <= line_count:
        mapping[position] = new_line
        position += 1
        new_line += 1
    return mapping


def _enum_attribute(node, names):
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in names
        and isinstance(node.value.ctx, ast.Load)
    )


def _is_none(node):
    return isinstance(node, ast.Constant) and node.value is None


def _locally_bound(tree, line, name):
    """True when the function around `line` binds `name`, so it is not the imported Enum."""
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if not hasattr(child, "lineno") or not child.lineno <= line <= child.end_lineno:
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = child.args
                bound = {arg.arg for arg in args.posonlyargs + args.args + args.kwonlyargs}
                if args.vararg:
                    bound.add(args.vararg.arg)
                if args.kwarg:
                    bound.add(args.kwarg.arg)
                for sub in ast.walk(child):
                    if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store) and sub.id == name:
                        bound.add(name)
                        break
                if name in bound:
                    return True
                stack.append(child)
                break
            stack.append(child)
            break
    return False


def _canon_compare(node):
    clone = copy.deepcopy(node)
    for child in ast.walk(clone):
        if not isinstance(child, ast.Compare):
            continue
        child.ops = [
            ast.Eq() if isinstance(op, ast.Is) else ast.NotEq() if isinstance(op, ast.IsNot) else op
            for op in child.ops
        ]
    return clone


def _swapped_compares(old, new):
    """Compares whose operator differs, walked in lockstep. Empty when the shape differs."""
    found = []

    def walk(left, right):
        if isinstance(left, ast.cmpop) and isinstance(right, ast.cmpop):
            return True  # Eq versus Is is the swap, not a different shape
        if type(left) is not type(right):
            return False
        if isinstance(left, ast.Compare):
            if len(left.ops) != len(right.ops) or len(left.comparators) != len(right.comparators):
                return False
            if any(type(a) is not type(b) for a, b in zip(left.ops, right.ops)):
                found.append((left, right))
        children, others = list(ast.iter_child_nodes(left)), list(ast.iter_child_nodes(right))
        if len(children) != len(others):
            return False
        return all(walk(child, other) for child, other in zip(children, others))

    if not walk(old, new):
        return None
    return found


def _enclosing_function(tree, line):
    found = None
    stack = [tree]
    while stack:
        node = stack.pop()
        for child in ast.iter_child_nodes(node):
            if not hasattr(child, "lineno") or not child.lineno <= line <= child.end_lineno:
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found = child
            stack.append(child)
            break
    return found


def _function_nodes(fn):
    """Statements in `fn`, not statements of a nested function or class."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(ast.iter_child_nodes(node))


def _optional_or_union(node):
    if isinstance(node, ast.Name):
        return node.id in {"Optional", "Union"}
    return isinstance(node, ast.Attribute) and node.attr in {"Optional", "Union"}


def _annotation_ok(node, scalars):
    """True when the annotation is only str/int/bool/Token/TokenType, or None."""
    if isinstance(node, ast.Constant):
        return node.value is None
    if isinstance(node, ast.Name):
        return node.id in {"str", "int", "bool"} or node.id in scalars
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _annotation_ok(node.left, scalars) and _annotation_ok(node.right, scalars)
    if isinstance(node, ast.Subscript) and _optional_or_union(node.value):
        if isinstance(node.slice, ast.Tuple):
            return all(_annotation_ok(elt, scalars) for elt in node.slice.elts)
        return _annotation_ok(node.slice, scalars)
    return False


def _obvious_scalar(node, scalars):
    """An assignment that can only produce str, int, bool, Token, TokenType, or None."""
    if isinstance(node, ast.Constant):
        return node.value is None or isinstance(node.value, (str, bool, int))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = node.operand
        return isinstance(operand, ast.Constant) and isinstance(operand.value, int) and not isinstance(operand.value, bool)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return True
    if isinstance(node, (ast.Compare, ast.BoolOp)):
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"str", "int", "bool", "len"}:
        return True
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in scalars:
        return True
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in scalars
        and isinstance(node.value.ctx, ast.Load)
    )


def _stored_names(target):
    """Names a target stores, and whether it is a single plain name."""
    if isinstance(target, ast.Name) and isinstance(target.ctx, ast.Store):
        return {target.id}, True
    return {node.id for node in ast.walk(target) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}, False


def _none_operand_ok(node, tree, line, scalars):
    """The other side of `is None` is a name this function types as a scalar.

    Anything else, including an attribute and an unannotated parameter, is left as a real change.
    """
    if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load):
        return False
    fn = _enclosing_function(tree, line)
    if fn is None:
        return False
    name, annotations, values = node.id, [], []
    for arg in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
        if arg.arg == name and arg.annotation is not None:
            annotations.append(arg.annotation)
    for stmt in _function_nodes(fn):
        if isinstance(stmt, ast.AnnAssign):
            stored, plain = _stored_names(stmt.target)
            if name not in stored:
                continue
            if not plain:
                return False
            if stmt.annotation is not None:
                annotations.append(stmt.annotation)
            if stmt.value is not None:
                values.append(stmt.value)
        elif isinstance(stmt, ast.Assign):
            hit = False
            for target in stmt.targets:
                stored, plain = _stored_names(target)
                if name not in stored:
                    continue
                if not plain:
                    return False
                hit = True
            if hit:
                values.append(stmt.value)
        elif isinstance(stmt, (ast.AugAssign, ast.For, ast.NamedExpr)):
            stored, _ = _stored_names(stmt.target)
            if name in stored:
                return False
    if any(not _annotation_ok(ann, scalars) for ann in annotations):
        return False
    if any(not _obvious_scalar(value, scalars) for value in values):
        return False
    return bool(annotations) or bool(values)


def _enum_equivalent(old, new, names, tree, line, scalars):
    """The statements differ only by is↔== or is not↔!= on a plain Enum, or on None.

    None applies only when the other operand is a str, int, bool, Token, or TokenType in this
    function. An enum that overrides equality is not in `names`.
    """
    if ast.dump(_canon_compare(old)) != ast.dump(_canon_compare(new)):
        return False
    swaps = _swapped_compares(old, new)
    if not swaps:
        return False
    for left, right in swaps:
        sides = [left.left, *left.comparators]
        for index, (before, after) in enumerate(zip(left.ops, right.ops)):
            if type(before) is type(after):
                continue
            if (type(before), type(after)) not in _ENUM_SWAP:
                return False
            pair = (sides[index], sides[index + 1])
            if any(_is_none(side) for side in pair):
                others = [side for side in pair if not _is_none(side)]
                if len(others) != 1 or not _none_operand_ok(others[0], tree, line, scalars):
                    return False
                continue
            attrs = [side for side in pair if _enum_attribute(side, names)]
            if not attrs or any(_locally_bound(tree, line, side.value.id) for side in attrs):
                return False
    return True


def _equivalent_lines(tree, new_tree, hunks, old_lines, changed, unit, names, scalars):
    """Old lines of statements whose only change is a safe enum or None comparison swap."""
    mapping, skip, seen = _line_map(hunks, len(old_lines)), set(), set()
    for line in changed:
        statement = unit(line)
        if not statement:
            continue
        first, last, node = statement
        if first in seen:
            continue
        seen.add(first)
        new_line = mapping.get(line)
        if new_line is None:
            continue
        new_statement = _unit(new_tree, new_line)
        if not new_statement:
            continue
        if _enum_equivalent(node, new_statement[2], names, tree, line, scalars):
            skip.update(range(first, last + 1))
    return skip


def _statement_span(statements):
    if not statements:
        return set()
    start = min(stmt.lineno for stmt in statements)
    end = max(stmt.end_lineno for stmt in statements)
    return set(range(start, end + 1))


def _line_span(node):
    if node is None:
        return set()
    return set(range(node.lineno, node.end_lineno + 1))


def _dead_bodies(tree, table, imported):
    """Bodies of if/elif/else/except/loop whose lines no test runs, with their condition lines.

    A body qualifies when none of its lines is in the line map or in import_lines. The condition
    is the header: the test, the for/while header, or the except type. An else has no test; its
    header is the else line, so an edit of that line is not a body edit.
    """
    found = []

    def add(body, condition):
        span = _statement_span(body)
        if not span:
            return
        if any(str(line) in table or line in imported for line in span):
            return
        found.append((span, condition))

    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            add(node.body, set(range(node.lineno, node.body[0].lineno)) | _line_span(node.test))
            if node.orelse and not (len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If)):
                add(node.orelse, {node.orelse[0].lineno - 1})
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
            header = set(range(node.lineno, node.body[0].lineno))
            header |= _line_span(node.iter) if isinstance(node, (ast.For, ast.AsyncFor)) else _line_span(node.test)
            if isinstance(node, (ast.For, ast.AsyncFor)):
                header |= _line_span(node.target)
            add(node.body, header)
            if node.orelse:
                add(node.orelse, {node.orelse[0].lineno - 1})
        elif isinstance(node, ast.ExceptHandler) and node.body:
            header = set(range(node.lineno, node.body[0].lineno)) | _line_span(node.type)
            add(node.body, header)
        elif isinstance(node, ast.Try) and node.orelse:
            add(node.orelse, {node.orelse[0].lineno - 1})
    return found


def _edited_old_lines(hunks):
    """Old line numbers the patch removes, plus the line a pure insertion is placed before."""
    lines = []
    for rows in hunks:
        index = 0
        while index < len(rows):
            if rows[index][0] == " ":
                index += 1
                continue
            start = index
            while index < len(rows) and rows[index][0] != " ":
                index += 1
            block = rows[start:index]
            removed = [number for kind, number, _ in block if kind == "-"]
            if removed:
                lines.extend(removed)
            else:
                lines.append(block[0][1])
    return lines


_DEAD_BODIES = {}


def _dead_edit(path, tree, hunks, table, imported):
    """Every changed line is in a dead body, and the condition line is untouched."""
    edited = _edited_old_lines(hunks)
    if not edited:
        return False
    if any(str(line) in table or line in imported for line in edited):
        return False  # a covered or import line is not a dead body
    bodies = _DEAD_BODIES.get(path)
    if bodies is None:
        bodies = _dead_bodies(tree, table, imported)
        _DEAD_BODIES[path] = bodies
    if not bodies:
        return False
    for line in edited:
        if not any(line in span and line not in condition and not (set(edited) & condition) for span, condition in bodies):
            return False
    return True


def _where(path, first, last):
    """`path:line` or `path:first-last`. No line number leaves just the path."""
    if first is None or last is None:
        return path
    if first == last:
        return f"{path}:{first}"
    return f"{path}:{first}-{last}"


def _note(blame, rule, where, tests):
    """Record which rule and line added `tests`. Callers never read this back into the selection."""
    if blame is None or not tests:
        return
    if not isinstance(tests, frozenset):
        tests = set(tests)
    if tests:
        blame.append((rule, where, tests))


def _span_wheres(path, spans):
    if not spans:
        return [path]
    return [_where(path, first, last) for first, last in spans]


def _owner_class(tree, line):
    """Innermost class whose span contains `line`, including a line inside one of its methods."""
    best = None

    def walk(node, prefix):
        nonlocal best
        for child in ast.iter_child_nodes(node):
            if not hasattr(child, "lineno"):
                continue
            end = getattr(child, "end_lineno", child.lineno)
            if not child.lineno <= line <= end:
                continue
            if isinstance(child, ast.ClassDef):
                qual = prefix + [child.name]
                span = end - child.lineno
                if best is None or span < best[0]:
                    best = (span, qual)
                walk(child, qual)
            else:
                walk(child, prefix)

    walk(tree, [])
    return ".".join(best[1]) if best else None


def _edited_classes(files, maps):
    """module:QualName of every class a hunk touches, methods included."""
    found = set()
    for file in files:
        path = file.old
        if not path or file.binary or not file.hunks or not path.endswith(".py"):
            continue
        if not path.startswith("sqlglot/"):
            continue
        try:
            _text, tree, _dump = maps.source(path)
        except (OSError, SyntaxError, ValueError):
            continue
        lines = set()
        for rows in file.hunks:
            for kind, number, _row in rows:
                if kind in ("-", "+") and number:
                    lines.add(number)
        for line in lines:
            qual = _owner_class(tree, line)
            if qual:
                found.add(f"{_module_name(path)}:{qual}")
    return found


def _key_owner(key, classes):
    """The edited class a fingerprint key belongs to, or None when it sits outside all of them."""
    module, _, rest = key.partition(":")
    owner = None
    for cls in classes:
        class_module, _, name = cls.partition(":")
        if module == class_module and (rest == name or rest.startswith(name + ".")):
            if owner is None or len(name) > len(owner[1]):
                owner = (cls, name)
    return owner[0] if owner else None


def _fingerprint_decision(original, patched, classes):
    """Narrow only when every changed key lives on a class this patch edited."""
    changed = set()
    for key in set(original) | set(patched):
        if original.get(key) == patched.get(key):
            continue
        owner = _key_owner(key, classes)
        if owner is None:
            return "all", ()
        changed.add(owner)
    return "narrow", tuple(sorted(changed))


def _cpu_seconds():
    total = 0.0
    for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN):
        usage = resource.getrusage(who)
        total += usage.ru_utime + usage.ru_stime
    return total


_TREE = "/tmp/s"
_TREE_STATE = {"ready": False}


def _python_env():
    return {"PYTHONPATH": _TREE, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONHASHSEED": "0"}


def _prepare_tree(files, maps):
    """Copy the package to /tmp/s and apply the patch. Reused by both checks."""
    if _TREE_STATE["ready"]:
        return True
    if os.path.isdir(_TREE):
        shutil.rmtree(_TREE)
    shutil.copytree(
        os.path.join(maps.source_dir, "sqlglot"),
        os.path.join(_TREE, "sqlglot"),
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for file in files:
        path = file.old or file.new
        if not path or file.binary or not path.startswith("sqlglot/"):
            continue
        full = os.path.join(_TREE, path)
        if file.old is None:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            text = ""
        elif not os.path.isfile(full):
            return False
        else:
            with open(full, encoding="utf-8") as handle:
                text = handle.read()
        if file.new is None:
            os.remove(full)
            continue
        written = apply(text, file.hunks) if file.hunks else text
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(written)
    _TREE_STATE["ready"] = True
    return True


def _cleanup_tree():
    shutil.rmtree(_TREE, ignore_errors=True)
    _TREE_STATE["ready"] = False


def _run_python(script, stdin=None, timeout=30):
    return subprocess.run(
        [sys.executable, "-S", script],
        cwd="/tmp",
        env=_python_env(),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _json_out(stdout):
    start = stdout.find("{")
    if start < 0:
        return None
    return json.loads(stdout[start:])


def _patched_fingerprint(files, maps, keep=False):
    """Copy the package, apply the patch, import it. None means the import did not survive."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fingerprint.py")
    try:
        if not _prepare_tree(files, maps):
            return None
        proc = _run_python(script)
        if proc.returncode != 0:
            return None
        return _json_out(proc.stdout)
    finally:
        if not keep:
            _cleanup_tree()


def _run_import_check(files, maps, keep=False):
    """-> (decision, changed classes, cpu seconds). A failed import selects every test."""
    start = _cpu_seconds()
    try:
        patched = _patched_fingerprint(files, maps, keep)
        if patched is None:
            return "failed", (), _cpu_seconds() - start
        if maps._import_fp is None:
            with open(os.path.join(maps.maps_dir, "import_fp.json"), encoding="utf-8") as handle:
                maps._import_fp = json.load(handle)
        decision, classes = _fingerprint_decision(maps._import_fp, patched, _edited_classes(files, maps))
        return decision, classes, _cpu_seconds() - start
    except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return "all", (), _cpu_seconds() - start


# --- rules 1-6: sqlglot/**/*.py -------------------------------------------------------------
def _source(path, hunks, maps, blame=None, check=None):
    tests = set(maps.py_reads.get(path, ()))  # rule 7 also holds for a source that tests read as text
    _note(blame, "py-read", path, tests)
    narrowed_import = False

    def keep_all():
        """False when the import fingerprint changed only inside a class this patch edited."""
        nonlocal narrowed_import
        if check is None:
            return True
        decision, classes, _cpu = check()
        if decision != "narrow":
            return True
        narrowed_import = True
        for key in classes:
            inst = maps.inst.get(key) or ()
            if _class_cap() and len(inst) > _CLASS_CAP:
                union = _class_union(key, maps)
                if union is not None:
                    inst = union
            tests.update(inst)
            _note(blame, "class", key, inst)
        return False
    text, tree, dump = maps.source(path)
    try:
        # Parsed from bytes, as the import system does, so a broken encoding line is an error here too
        new_tree = ast.parse(apply(text, hunks).encode("utf-8"))
        same = ast.dump(new_tree) == dump
    except (SyntaxError, ValueError, UnicodeError) as error:
        _note(blame, f"all:{type(error).__name__}", path, maps.everything)
        return maps.everything, f"all:{type(error).__name__}"  # the module no longer imports
    if same:
        return tests, "noop"  # rule 1: only comments or layout changed
    if _equiv_dead() and _dead_edit(path, tree, hunks, maps.lines(path), maps.imported(path)):
        return tests, "equiv-dead"  # the body never runs; keep only tests that read the file as text

    units = {}

    def unit(line):
        if line not in units:
            units[line] = _unit(tree, line)
        return units[line]

    old_lines = text.split("\n")
    table, imported = maps.lines(path), maps.imported(path)
    indexes, at_import, docstring = set(), False, False
    class_keys, import_ran, module_import = set(), False, False
    doc_spans, class_spans, union_where, word_at = [], {}, [], {}
    changed, neighbor_lines = _changed(hunks, unit, len(old_lines))
    skip = set()
    if _equiv_enum():
        skip = _equivalent_lines(
            tree, new_tree, hunks, old_lines, changed, unit, maps.enum_names(path), maps.scalar_names(path)
        )
    for line, new in changed.items():  # rule 2
        if line in skip:
            continue
        first, last, node = unit(line)
        if isinstance(node, _DEFINITIONS):
            last = node.end_lineno  # a def, class or decorator line: everything in its body
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            is_doc = isinstance(node.value.value, str)
            docstring = docstring or is_doc
            if blame is not None and is_doc:
                doc_spans.append((first, last))
        inside = isinstance(node, ast.Lambda) and _within_lambda_body(node, line, old_lines[line - 1], new)
        covered, span_import = False, False
        for number in range(first, last + 1):
            index = table.get(str(number))
            if index is not None:
                indexes.add(index)  # rule 3: tests that ran the statement
                covered = True
            if number in imported:
                span_import = True
                if not inside:
                    at_import = True  # rule 4: it also runs when the module is imported
        if blame is not None and covered:
            found = set()
            for number in range(first, last + 1):
                index = table.get(str(number))
                if index is not None:
                    found.update(maps.tests_of(index))
            _note(blame, "line", _where(path, first, last), found)
        if span_import and _in_function(tree, line):
            import_ran = True  # a function that runs at import can break every importer
            _note(blame, "all:import-line", _where(path, first, last), maps.everything)
        elif span_import:
            chain = _class_chain(tree, line)
            if chain:
                key = f"{_module_name(path)}:{chain}"
                class_keys.add(key)
                if blame is not None:
                    class_spans.setdefault(key, []).append((first, last))
            else:
                module_import = True  # an assignment or other statement that is not inside a class
                if blame is not None:
                    union_where.append(_where(path, first, last))
                if covered:
                    import_ran = True  # module-level code that tests ran and that also runs at import
                    _note(blame, "all:import-line", _where(path, first, last), maps.everything)
    if blame is not None and neighbor_lines:
        def covered_by(lines):
            found = set()
            for line in lines:
                if line in skip:
                    continue
                first, last, node = unit(line)
                if isinstance(node, _DEFINITIONS):
                    last = node.end_lineno
                for number in range(first, last + 1):
                    index = table.get(str(number))
                    if index is not None:
                        found.update(maps.tests_of(index))
            return found
        only = covered_by(neighbor_lines) - covered_by(set(changed) - neighbor_lines)
        if only:
            _note(blame, "neighbor", path, only)
    for index in indexes:
        tests.update(maps.tests_of(index))
    reason = "line" if indexes else "uncovered"  # rule 5: a line that never ran selects nothing
    edits, word_bag, narrowed = None, set(), False
    # file_union stays for module-level import lines, for a class with no inst entry, and when
    # a changed literal is not a word. A class created at import still selects every test.
    need_union = module_import
    for key in class_keys:
        if key in maps.import_classes:
            if keep_all():
                for other in class_keys:
                    if other in maps.import_classes:
                        for where in _span_wheres(path, class_spans.get(other)):
                            _note(blame, "all:import-class", where, maps.everything)
                return maps.everything, "all:import-class"
            continue
        inst_tests = maps.inst.get(key)
        info = None
        if _narrow_class() and inst_tests:
            if edits is None:
                edits = _class_edits(hunks, tree, path)
            info = edits.get(key)
        wheres = _span_wheres(path, class_spans.get(key))
        if info is not None and info[1]:
            tests.update(inst_tests)
            word_bag |= info[0]
            narrowed = True
            for where in wheres:
                _note(blame, "class", where, inst_tests)
                if blame is not None and info[0]:
                    word_at.setdefault(where, set()).update(info[0])
        else:
            need_union = True
            tests.update(inst_tests or ())
            for where in wheres:
                _note(blame, "class", where, inst_tests or ())
                if blame is not None:
                    union_where.append(where)
    if import_ran and keep_all():
        return maps.everything, "all:import-line"
    if at_import and need_union:
        if path not in maps.file_union:
            _note(blame, "all:import-unmapped", path, maps.everything)
            return maps.everything, "all:import-unmapped"  # imported, yet no test is known to use it
        union = set(maps.file_union[path])
        if indexes:
            _note(blame, "dialect", path, (_dialect_tests(path, maps) & union) - tests)
        if _dialect_file_off() and indexes:
            union -= _dialect_tests(path, maps) - tests
        tests.update(union)
        tests.update(maps.blind())
        for where in union_where or [path]:
            _note(blame, "import->file", where, union)
            _note(blame, "blind", where, maps.blind())
        reason = "import->file"
        if class_keys:
            reason += "+class"
    elif narrowed:
        reason = "line+class" if indexes else "class"
    if word_bag:
        tests.update(maps.words_of(word_bag))
        if blame is not None:
            for where, words in word_at.items():
                _note(blame, "word", where, maps.words_of(words))
        reason += "+word"
    if docstring:
        doc = maps.doctests()
        tests.update(doc)
        for where in _span_wheres(path, doc_spans):
            _note(blame, "docstring", where, doc)
        reason += "+docstring"
    if skip and changed and all(line in skip for line in changed) and reason == "uncovered":
        reason = "equiv-enum"
    if narrowed_import and not reason.startswith("all:"):
        reason += "+import-check"
    return tests, reason


def _new_source(file, files, maps, blame=None):
    """A new module under sqlglot/: nothing at the pinned commit imports it, so it only matters
    when the same patch starts to use it, or when a test reads every module of its folder."""
    name = os.path.basename(file.new)[:-3]
    if name == "__init__" or os.path.exists(os.path.join(maps.source_dir, file.new)):
        _note(blame, "all:new-package", file.new, maps.everything)
        return maps.everything, "all:new-package"
    for other in files:
        if other is not file and any(kind == "+" and name in row for rows in other.hunks for kind, _, row in rows):
            _note(blame, "all:new-imported", file.new, maps.everything)
            return maps.everything, "all:new-imported"
    folder, tests = os.path.dirname(file.new), set()
    for path, numbers in maps.py_reads.items():
        if os.path.dirname(path) == folder:
            tests.update(numbers)
            _note(blame, "new", path, numbers)
    return tests, "new"


# --- rule 7: fixtures and other data files --------------------------------------------------
def _data(path, maps, blame=None):
    if path in maps.opens:
        tests = set(maps.opens[path])
        _note(blame, "data", path, tests)
        return tests
    # No test opened it by name (a new file, or one found by listing a folder): take the tests
    # that opened its nearest neighbours.
    folder = os.path.dirname(path)
    while folder and folder != "tests":
        tests = set()
        for other, numbers in maps.opens.items():
            if other.startswith(folder + "/"):
                tests.update(numbers)
        if tests:
            _note(blame, "data", folder + "/", tests)
            return tests
        folder = os.path.dirname(folder)
    _note(blame, "all:data", path, maps.everything)
    return maps.everything


# --- rule 8: tests/**/*.py ------------------------------------------------------------------
def _test(path, maps, blame=None):
    if path not in maps.testfile:
        _note(blame, "all:test-support", path, maps.everything)
        return maps.everything, "all:test-support"  # helpers, conftest, __init__: any test may use it
    tests, parts = set(), []
    for file in maps.importers(path):
        if file not in maps.testfile:
            _note(blame, "all:test-support", file, maps.everything)
            return maps.everything, "all:test-support"
        numbers = maps.testfile[file]
        tests.update(numbers)
        parts.append((file, numbers))
    for file, numbers in parts:
        _note(blame, "testfile", file, numbers)
    return tests, "testfile"


def _file(file, files, maps, blame=None, check=None):
    paths = {path for path in (file.old, file.new) if path}
    kinds = {classify(path, maps.opens) for path in paths}
    if kinds == {"data"}:  # changed, added, deleted or renamed: every name it had or gets
        return set().union(*(_data(path, maps, blame) for path in paths)), "data"
    if file.old is None and kinds == {"source"}:
        return _new_source(file, files, maps, blame)
    if file.old != file.new or file.binary or not file.hunks:
        reason = "all:" + ("added-deleted-renamed" if file.old != file.new else "no-text-change")
        _note(blame, reason, file.old or file.new, maps.everything)
        return maps.everything, reason
    if kinds == {"source"}:
        return _source(file.old, file.hunks, maps, blame, check)
    if kinds == {"test"}:
        return _test(file.old, maps, blame)
    _note(blame, "all:other", file.old or file.new, maps.everything)
    return maps.everything, "all:other"  # rule 9


# Tests over 3s. Numbers are the pinned tests.json ids, not the leaf names (athena also has test_max_nodes).
_TPC_IDS = ((1106, "tpcds"), (1107, "tpch"), (1123, "opt_tpch"))
_MAX_ID = (1490, "max_nodes")
_GEN_ID = (1224, "gen_mem")
_MAX_FILES = frozenset({
    "sqlglot/parser.py",
    "sqlglot/tokens.py",
    "sqlglot/tokenizer_core.py",
    "sqlglot/expressions/core.py",
})
_GEN_FILES = frozenset({"sqlglot/generator.py", "sqlglot/expressions/core.py"})
_COVERAGE_RULES = frozenset({"line", "class", "import->file", "blind"})
_CHECK_CPU_LIMIT = 5.0


def _paths(files):
    found = set()
    for file in files:
        for path in (file.old, file.new):
            if path:
                found.add(path)
    return found


def _edited_lines(files):
    """Original `path:line` keys an edit touches. A '+' row names the old line it is inserted before."""
    found = set()
    for file in files:
        if not file.old:
            continue
        for rows in file.hunks:
            for kind, number, _row in rows:
                if kind != " ":
                    found.add(f"{file.old}:{number}")
    return found


def _fixture_keeps(number, files, maps):
    """A changed fixture, a source the test reads, or its own test file keeps the test."""
    for path in _paths(files):
        if (
            number in maps.opens.get(path, ())
            or number in maps.py_reads.get(path, ())
            or number in maps.testfile.get(path, ())
        ):
            return True
    return False


def _coverage_added(number, blame):
    for rule, _where, tests in blame:
        if number not in tests:
            continue
        if rule in _COVERAGE_RULES or rule.startswith("all:"):
            return True
    return False


def _can_drop(number, selected, files, maps, blame):
    if number not in selected or maps.durations[number] <= _SLOW_MS:
        return False
    if _fixture_keeps(number, files, maps):
        return False
    return _coverage_added(number, blame)


def _tpc_unchanged(files, maps):
    """True when patched optimize().sql() matches the pinned hashes. None keeps the tests."""
    paths = _paths(files)
    if any(path.startswith("sqlglot/executor/") for path in paths):
        return False
    data = maps.tpc()
    wanted, seen = [], set()
    for path in paths:
        if not (path.startswith("sqlglot/") and path.endswith(".py")):
            continue
        for query_id in data["files"].get(path, ()):
            if query_id not in seen:
                seen.add(query_id)
                wanted.append(query_id)
    if not wanted:
        return True
    if not _prepare_tree(files, maps):
        return None
    queries = data["queries"]
    schemas, payload = {}, []
    for query_id in wanted:
        row = queries[query_id]
        schemas[row["s"]] = data["schemas"][row["s"]]
        payload.append({"id": query_id, "sql": row["sql"], "schema": row["s"]})
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe.py")
    proc = _run_python(script, json.dumps({"schemas": schemas, "queries": payload}), timeout=6)
    if proc.returncode != 0:
        return None
    got = _json_out(proc.stdout)
    if not isinstance(got, dict):
        return None
    for query_id in wanted:
        if got.get(query_id) != queries[query_id]["hash"]:
            return False
    return True


def _keep_max_nodes(files, maps):
    if _paths(files) & _MAX_FILES:
        return True
    return bool(_edited_lines(files) & maps.max_node_lines())


def _keep_generator(files):
    return bool(_paths(files) & _GEN_FILES)


def _drop_expensive(selected, files, maps, blame, import_failed):
    """Remove expensive tests the patch cannot change. Returns the short names dropped and the CPU."""
    start = _cpu_seconds()
    names = []
    try:
        if not import_failed:
            tpc_ids = [number for number, _name in _TPC_IDS if _can_drop(number, selected, files, maps, blame)]
            if tpc_ids:
                same = _tpc_unchanged(files, maps)
                if same is True:
                    names.extend(name for number, name in _TPC_IDS if number in tpc_ids)
            if _can_drop(_MAX_ID[0], selected, files, maps, blame) and not _keep_max_nodes(files, maps):
                names.append(_MAX_ID[1])
            if _can_drop(_GEN_ID[0], selected, files, maps, blame) and not _keep_generator(files):
                names.append(_GEN_ID[1])
        _cleanup_tree()
        if import_failed or _cpu_seconds() - start > _CHECK_CPU_LIMIT:
            return [], _cpu_seconds() - start
        by_name = {name: number for number, name in (*_TPC_IDS, _MAX_ID, _GEN_ID)}
        for name in names:
            selected.discard(by_name[name])
        return names, _cpu_seconds() - start
    except (OSError, ValueError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, TypeError):
        _cleanup_tree()
        return [], _cpu_seconds() - start


def _risk_threshold():
    """RISK=<p> drops the least likely failures while their probabilities sum to less than p."""
    raw = os.environ.get("RISK", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


_FIXTURE_RULES = frozenset({"data", "py-read", "testfile"})


def _rule_kind(rule):
    """0 line, 1 class-level, 2 import-time, 3 fixture. None if the note is not one of these."""
    if rule in _FIXTURE_RULES:
        return 3
    if rule in {"all:import-line", "all:import-class", "all:import-unmapped", "import->file"}:
        return 2
    if rule == "class":
        return 1
    if rule in {"line", "word", "neighbor", "dialect", "blind", "docstring"}:
        return 0
    return None


def _condition_lines(tree):
    """Lines that belong to an if/elif test, not the body under it."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        start = getattr(test, "lineno", None)
        end = getattr(test, "end_lineno", start)
        if start is None:
            continue
        found.update(range(start, end + 1))
    return found


def _name_match(paths, test_file):
    """1 when duckdb.py matches test_duckdb.py, or optimizer/x.py matches test_optimizer.py."""
    stem = os.path.basename(test_file)
    if stem.startswith("test_") and stem.endswith(".py"):
        stem = stem[len("test_"):-len(".py")]
    else:
        return 0.0
    for path in paths:
        base = os.path.basename(path)
        if base.endswith(".py"):
            base = base[:-len(".py")]
        parent = os.path.basename(os.path.dirname(path))
        if stem == base or stem == parent:
            return 1.0
    return 0.0


def _changed_coverage(files, maps):
    """Changed lines the coverage map knows, as (tests, how many tests, condition?)."""
    rows, paths = [], []
    for file in files:
        for path in (file.old, file.new):
            if path:
                paths.append(path)
        path = file.old
        if not path or file.binary or not file.hunks or not path.endswith(".py"):
            continue
        try:
            text, tree, _dump = maps.source(path)
        except OSError:
            continue
        line_count = len(text.split("\n"))

        def unit(line, _tree=tree):
            return _unit(_tree, line)

        changed, _neighbors = _changed(file.hunks, unit, line_count)
        skip = set()
        if _equiv_enum():
            try:
                new_tree = ast.parse(apply(text, file.hunks).encode("utf-8"))
                old_lines = text.split("\n")
                skip = _equivalent_lines(
                    tree, new_tree, file.hunks, old_lines, changed, unit,
                    maps.enum_names(path), maps.scalar_names(path),
                )
            except (SyntaxError, ValueError, UnicodeError):
                skip = set()
        conditions = _condition_lines(tree)
        table = maps.lines(path)
        for line in changed:
            if line in skip:
                continue
            index = table.get(str(line))
            if index is None:
                continue
            numbers = set(maps.tests_of(index))
            rows.append((numbers, len(numbers), line in conditions))
    return rows, paths


def risk_features(files, maps, blame, tests):
    """Seven features for each candidate, in the order maps/risk.json was fit on.

    changed lines covered, log tests on those lines, log lines this test covers,
    name match, change kind (0 line, 1 class, 2 import-time, 3 fixture),
    log duration, condition line.
    """
    covered, paths = _changed_coverage(files, maps)
    breadth = maps.line_counts()
    kind = {}
    for rule, _where, numbers in blame or []:
        code = _rule_kind(rule)
        if code is None:
            continue
        for number in numbers:
            if code > kind.get(number, -1):
                kind[number] = code
    features = {}
    for test in tests:
        hits = [(size, cond) for numbers, size, cond in covered if test in numbers]
        count = len(hits)
        if count:
            spread = math.log1p(sum(size for size, _cond in hits) / count)
            condition = 1.0 if any(cond for _size, cond in hits) else 0.0
        else:
            spread = 0.0
            condition = 0.0
        features[test] = (
            float(count),
            spread,
            math.log1p(breadth[test] if test < len(breadth) else 0),
            _name_match(paths, maps.test_path(test)),
            float(kind.get(test, 0)),
            math.log1p(maps.durations[test] if test < len(maps.durations) else 0),
            condition,
        )
    return features


def fail_probability(weights, features):
    score = weights[0]
    for weight, value in zip(weights[1:], features):
        score += weight * value
    if score >= 0:
        return 1.0 / (1.0 + math.exp(-score))
    expo = math.exp(score)
    return expo / (1.0 + expo)


def _apply_risk(selected, files, maps, blame, threshold):
    """Drop the lowest P(fail) tests while their probabilities stay under the threshold."""
    try:
        weights = maps.risk_weights()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return None
    if len(weights) != 8 or not selected:
        return None
    protected = set()
    for rule, _where, numbers in blame or []:
        if rule in _FIXTURE_RULES:
            protected.update(number for number in numbers if number in selected)
    droppable = [test for test in selected if test not in protected]
    if not droppable:
        return None
    scored = []
    for test, features in risk_features(files, maps, blame, droppable).items():
        scored.append((fail_probability(weights, features), test))
    scored.sort()
    total = 0.0
    dropped = []
    for probability, test in scored:
        if total + probability >= threshold:
            break
        emptying = not protected and len(dropped) + 1 == len(scored)
        if emptying and total + probability >= threshold / 3:
            break
        total += probability
        dropped.append(test)
    for test in dropped:
        selected.discard(test)
    if not dropped:
        return None
    return f"risk={len(dropped)}:{total:.3f}"


_ALWAYS_SLOW = (
    ("tests/test_parser.py::TestParser::test_max_nodes", "max_nodes"),
    ("tests/test_generator.py::TestGenerator::test_generator_memory", "gen_mem"),
)
_TPC_SLOW = (
    ("tests/test_executor.py::TestExecutor::test_execute_tpcds", "tpcds"),
    ("tests/test_executor.py::TestExecutor::test_execute_tpch", "tpch"),
    ("tests/test_executor.py::TestExecutor::test_optimized_tpch", "opt_tpch"),
)
_TPC_KEEP_PREFIXES = ("sqlglot/optimizer/", "sqlglot/executor/", "sqlglot/planner")


def _tpc_kept(files):
    """The TPC executor tests stay when a changed path can reach their SQL."""
    for file in files:
        for path in (file.old, file.new):
            if not path:
                continue
            if path == "sqlglot/serde.py" or "tpc" in path or path.startswith(_TPC_KEEP_PREFIXES):
                return True
    return False


def _apply_slow_policy(selected, files, maps, reasons):
    """Last step. A failed import or fingerprint (any `all:` reason) keeps every slow test."""
    if not _slow_policy() or any("all:" in reason for reason in reasons):
        return None
    by_id = maps.by_id
    if not by_id:
        return None
    names = []
    for nodeid, name in _ALWAYS_SLOW:
        number = by_id.get(nodeid)
        if number is not None and number in selected:
            selected.discard(number)
            names.append(name)
    if not _tpc_kept(files):
        for nodeid, name in _TPC_SLOW:
            number = by_id.get(nodeid)
            if number is not None and number in selected:
                selected.discard(number)
                names.append(name)
    return "slow=" + "+".join(names) if names else None


def select(files, maps, blame=None):
    """-> (test numbers, [reason per file]): the union over the files of one patch.

    `blame`, when a list, receives (rule, line-range, tests) for each addition. It is not
    consulted. main.py leaves it empty, so a scored run records nothing.
    """
    selected, reasons = set(), []
    box = {}
    record = blame if blame is not None else ([] if _expensive_check() or _risk_threshold() is not None else None)

    def check():
        if "value" not in box:
            box["value"] = _run_import_check(files, maps, keep=_expensive_check())
        return box["value"]

    runner = check if _import_check() else None
    done = False
    try:
        for file in files:
            tests, reason = _file(file, files, maps, record, runner)
            if not done:
                selected.update(tests)
                reasons.append(reason)
                done = len(selected) == len(maps.everything)
            if done and not _expensive_check() and _risk_threshold() is None:
                break
        if "value" in box:
            reasons.append(f"check={box['value'][2]:.3f}s")
        if _expensive_check():
            failed = box.get("value", ("",))[0] == "failed"
            dropped, cpu = _drop_expensive(selected, files, maps, record or [], failed)
            if dropped:
                reasons.append("drop=" + "+".join(dropped))
            reasons.append(f"xcheck={cpu:.3f}s")
        threshold = _risk_threshold()
        if threshold is not None and threshold > 0:
            note = _apply_risk(selected, files, maps, record or [], threshold)
            if note:
                reasons.append(note)
        note = _apply_slow_policy(selected, files, maps, reasons)
        if note:
            reasons.append(note)
    finally:
        _cleanup_tree()
    return selected, reasons
