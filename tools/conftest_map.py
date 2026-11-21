"""Map build only: tools/build_map.sh copies this to sqlglot/conftest.py. Never ships in the image.

It makes one coverage run see what a real test run depends on:
1. ProcessPoolExecutor runs inline, so pool work is credited to the calling test. It still runs
   the initializer and pickles arguments and results, as a process boundary does
   (Expr.__reduce__ -> sqlglot/serde.py is reached no other way).
2. Every sqlglot module is imported before collection, so module and class-body lines get the
   empty coverage context instead of whichever test first lazy-loads a dialect.
3. Per test it records non-.py files opened (maps/opens.json) and, in maps/trace.json, the
   lazily imported sqlglot modules it depends on (users) and .py sources it read as text
   (py_reads). "Lazily imported" = not loaded by a bare `import sqlglot`: a test depends on such
   a module's import-time lines only if it asks for the module, directly or through a dialect.

A recorded "test" is a scope: a test id, `file::Class` for class-level setup or teardown,
a test file for work done while it is imported, or "" when nothing narrower is known.
"""
import builtins
import concurrent.futures as cf
import importlib
import importlib.util
import json
import os
import pathlib
import pickle
import pkgutil
import sys

ROOT = os.path.dirname(os.path.realpath(__file__))  # the sqlglot checkout
MAPS = os.path.join(os.path.dirname(ROOT), "maps")
TESTS = os.path.join(ROOT, "tests") + os.sep

# tests spawn `python`; the venv is not activated during the build
os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")


class InlineExecutor(cf.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kwargs):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *a, **k):
        f = cf.Future()
        try:
            fn, a, k = pickle.loads(pickle.dumps((fn, a, k)))
            f.set_result(pickle.loads(pickle.dumps(fn(*a, **k))))
        except BaseException as e:
            f.set_exception(e)
        return f

    def shutdown(self, *a, **k):
        pass


cf.ProcessPoolExecutor = InlineExecutor


def scope():
    current = os.environ.get("PYTEST_CURRENT_TEST")
    if current:
        nodeid, _, phase = current.rpartition(" ")
        # setUpClass runs in the first test's setup phase but feeds every test of the class
        return nodeid if phase == "(call)" else nodeid.rpartition("::")[0]
    found, frame = "", sys._getframe(1)
    while frame:  # no test is running: a test file is being imported (outermost one wins)
        name = frame.f_code.co_filename
        if name.startswith(TESTS) and os.path.basename(name).startswith("test_"):
            found = os.path.relpath(name, ROOT).replace(os.sep, "/")
        frame = frame.f_back
    return found


# --- files opened -------------------------------------------------------------------------
OPENS = {}  # repo-relative path -> scopes


def note_open(file):
    try:
        if isinstance(file, int):
            return
        path = os.path.realpath(os.fsdecode(file))
        if path.startswith(ROOT + os.sep):
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if rel.startswith(".") or "__pycache__" in rel:
                return
            # outside a test, .py files are read by pytest itself to import them
            if not rel.endswith(".py") or os.environ.get("PYTEST_CURRENT_TEST"):
                OPENS.setdefault(rel, set()).add(scope())
    except Exception:
        pass


_open, _path_open, _path_read_text = builtins.open, pathlib.Path.open, pathlib.Path.read_text


def traced_open(file, *a, **k):
    note_open(file)
    return _open(file, *a, **k)


def traced_path_open(self, *a, **k):  # Path.open calls io.open, not builtins.open
    note_open(self)
    return _path_open(self, *a, **k)


def traced_path_read_text(self, *a, **k):
    note_open(self)
    return _path_read_text(self, *a, **k)


builtins.open = traced_open
pathlib.Path.open = traced_path_open
pathlib.Path.read_text = traced_path_read_text


# --- lazily imported modules used ---------------------------------------------------------
def ours(name):
    return name == "sqlglot" or name.startswith("sqlglot.")


import sqlglot  # noqa: E402

EAGER = {m for m in sys.modules if ours(m)}  # what a bare `import sqlglot` loads
EDGES = {}  # module -> modules its import-time code imports
ROOTS = {}  # scope -> modules it asked for
_import, _import_module = builtins.__import__, importlib.import_module
_seen = {}
_importing = 0  # traced imports in progress: above 0, some module's import-time code may be running


def note_import(frame, wanted):
    """`frame` runs the import. Import-time code of a sqlglot module -> an edge from that module;
    anything else (a test, a test file, a lazy __getattr__ called by one) -> a root of its scope."""
    wanted = {m for m in wanted if ours(m)}
    if not wanted:
        return
    while frame and _importing:  # the nearest module body up the stack is what asked
        if frame.f_code.co_name == "<module>":
            importer = frame.f_globals.get("__name__") or ""
            if ours(importer):
                EDGES.setdefault(importer, set()).update(wanted)
                return
            break
        frame = frame.f_back
    ROOTS.setdefault(scope(), set()).update(wanted)


def traced_import(name, globals=None, locals=None, fromlist=(), level=0):
    global _importing
    if globals is None or not (level or name.startswith("sqlglot")):
        return _import(name, globals, locals, fromlist, level)
    _importing += 1
    try:
        module = _import(name, globals, locals, fromlist, level)
    finally:
        _importing -= 1
    try:
        key = (globals.get("__name__"), name, level, tuple(fromlist or ()))
        wanted = _seen.get(key)
        if wanted is None:
            full = name
            if level:
                full = importlib.util.resolve_name("." * level + name, globals.get("__package__"))
            wanted = {full}
            for item in fromlist or ():  # a submodule, or an object defined in some module
                attr = getattr(module, item, None)
                is_module = isinstance(attr, type(sys))
                origin = attr.__name__ if is_module else getattr(attr, "__module__", None)
                if isinstance(origin, str):
                    wanted.add(origin)
            _seen[key] = wanted
        note_import(sys._getframe(1), wanted)
    except Exception:
        pass
    return module


def traced_import_module(name, package=None):
    global _importing
    _importing += 1
    try:
        module = _import_module(name, package)
    finally:
        _importing -= 1
    try:
        note_import(sys._getframe(1), {module.__name__})
    except Exception:
        pass
    return module


builtins.__import__ = traced_import
importlib.import_module = traced_import_module

_importing += 1
for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if not info.name.endswith("__main__"):
        try:
            _import_module(info.name)
        except Exception as error:
            print(f"conftest_map: cannot preload {info.name}: {error!r}", file=sys.stderr)
_importing -= 1

LAZY = {m for m in sys.modules if ours(m)} - EAGER

from sqlglot.dialects.dialect import Dialect, _Dialect  # noqa: E402

_class_modules = {}


def note_dialect(cls):
    """Using a dialect depends on the import-time code of every module its classes come from."""
    try:
        modules = _class_modules.get(cls)
        if modules is None:
            parts = [cls] + [
                getattr(cls, name, None)
                for name in ("tokenizer_class", "jsonpath_tokenizer_class", "parser_class", "generator_class")
            ]
            modules = {base.__module__ for part in parts for base in getattr(part, "__mro__", ())}
            modules = _class_modules[cls] = {m for m in modules if m in LAZY}
        if modules:
            ROOTS.setdefault(scope(), set()).update(modules)
    except Exception:
        pass


_init = Dialect.__init__
_get_or_raise = Dialect.__dict__["get_or_raise"].__func__


def traced_init(self, **kwargs):
    note_dialect(type(self))
    _init(self, **kwargs)


def traced_get_or_raise(cls, dialect):
    result = _get_or_raise(cls, dialect)
    note_dialect(type(result))
    return result


Dialect.__init__ = traced_init
Dialect.get_or_raise = classmethod(traced_get_or_raise)

for _name in ("get", "__getitem__"):  # registry lookups that hand out the class itself

    def traced_lookup(cls, key, *rest, _lookup=_Dialect.__dict__[_name].__func__, **named):
        result = _lookup(cls, key, *rest, **named)
        if isinstance(result, type):
            note_dialect(result)
        return result

    setattr(_Dialect, _name, classmethod(traced_lookup))


def _write(name, data):
    os.makedirs(MAPS, exist_ok=True)
    with _open(os.path.join(MAPS, name), "w") as f:
        json.dump(data, f, sort_keys=True, separators=(",", ":"))


def _listed(mapping):
    return {key: sorted(scopes) for key, scopes in mapping.items()}


def pytest_sessionfinish(session):
    files = {}  # lazily imported module -> its file
    for module in LAZY:
        file = getattr(sys.modules.get(module), "__file__", None)
        if file:
            files[module] = os.path.relpath(os.path.realpath(file), ROOT).replace(os.sep, "/")
    modules = sorted(set(files.values()))
    users = {}  # scope -> indexes into modules: every lazily imported file it depends on
    for sc, roots in ROOTS.items():
        reached, todo = set(), list(roots)
        while todo:
            module = todo.pop()
            if module not in reached:
                reached.add(module)
                todo.extend(EDGES.get(module, ()))
                if "." in module:
                    todo.append(module.rpartition(".")[0])  # importing a.b.c imports a.b
        used = sorted({modules.index(files[m]) for m in reached if m in files})
        if used:
            users[sc] = used
    _write("opens.json", _listed({p: s for p, s in OPENS.items() if not p.endswith(".py")}))
    _write(
        "trace.json",
        {
            "modules": modules,
            "users": users,
            "py_reads": _listed({p: s for p, s in OPENS.items() if p.endswith(".py")}),
        },
    )
