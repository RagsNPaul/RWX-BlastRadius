"""Import fingerprint of a sqlglot tree.

Run with `python -S` and the package on PYTHONPATH. Prints one JSON object,
key to sha256, and nothing else. The selector compares it with maps/import_fp.json.
"""
import enum
import hashlib
import importlib
import json
import os
import pkgutil
import re
import sys

_ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")


def _canon(value):
    if isinstance(value, dict):
        parts = sorted((_canon(key), _canon(item)) for key, item in value.items())
        return "{" + ",".join(f"{key}:{item}" for key, item in parts) + "}"
    if isinstance(value, (set, frozenset)):
        return "{" + ",".join(sorted(_canon(item) for item in value)) + "}"
    if isinstance(value, tuple):
        return "(" + ",".join(_canon(item) for item in value) + ")"
    if isinstance(value, list):
        return "[" + ",".join(_canon(item) for item in value) + "]"
    return _ADDRESS.sub("", repr(value))


def _hash(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _keep(value):
    return isinstance(value, (dict, set, frozenset, list, tuple, str, int, bool, enum.Enum)) and not isinstance(value, type)


def collect():
    import sqlglot
    import sqlglot.expressions as exp

    prefix = sqlglot.__name__ + "."
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix):
        if info.name.endswith(".__main__"):
            continue  # the console script expects an installed version and is not part of import
        importlib.import_module(info.name)

    found = {}

    def put(key, text):
        found[key] = _hash(text)

    def sqlglot_module(module):
        name = getattr(module, "__name__", "")
        return name == "sqlglot" or name.startswith(prefix)

    for module in list(sys.modules.values()):
        if not sqlglot_module(module):
            continue
        name = module.__name__
        for attr, value in vars(module).items():
            if attr.startswith("_"):
                continue
            if isinstance(value, exp.Expr):
                try:
                    put(f"{name}:{attr}.sql", value.sql())
                except Exception as error:
                    put(f"{name}:{attr}.sql", f"error:{type(error).__name__}:{error}")

    classes = []
    stack = [exp.Expression]
    while stack:
        cls = stack.pop()
        classes.append(cls)
        stack.extend(cls.__subclasses__())
    seen = set(classes)
    for module in list(sys.modules.values()):
        if not sqlglot_module(module):
            continue
        for value in vars(module).values():
            if isinstance(value, type) and value.__module__ == module.__name__ and value not in seen:
                seen.add(value)
                classes.append(value)

    for cls in classes:
        if not cls.__module__.startswith("sqlglot"):
            continue
        qual = f"{cls.__module__}:{cls.__qualname__}"
        for attr, value in cls.__dict__.items():
            if attr.startswith("__") and attr.endswith("__"):
                continue
            if _keep(value):
                put(f"{qual}.{attr}", _canon(value))
            elif isinstance(value, exp.Expr):
                try:
                    put(f"{qual}.{attr}.sql", value.sql())
                except Exception as error:
                    put(f"{qual}.{attr}.sql", f"error:{type(error).__name__}:{error}")
        if issubclass(cls, exp.Expression):
            arg_types = getattr(cls, "arg_types", None)
            sql_names = getattr(cls, "_sql_names", None)
            if isinstance(arg_types, dict):
                put(f"{qual}.arg_types", _canon(arg_types))
            if sql_names is not None and _keep(sql_names):
                put(f"{qual}._sql_names", _canon(sql_names))

    names = sorted(f"{cls.__module__}:{cls.__qualname__}" for cls in exp.ALL_FUNCTIONS)
    put("sqlglot.expressions:ALL_FUNCTIONS", _canon(names))
    return found


def main():
    json.dump(collect(), sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")


def _part(file, name):
    if isinstance(file, dict):
        return file[name]
    return getattr(file, name)


def prepare_tree(source_dir, files):
    """Copy the package to /tmp/s and apply the patch. False when an old file is missing.

    `files` are FilePatch objects or dicts with old, new, binary, and hunks. The copy always
    starts from `source_dir`, so a later commit does not inherit the previous patch.
    """
    import os
    import shutil

    from patch import apply

    tree = "/tmp/s"
    if os.path.isdir(tree):
        shutil.rmtree(tree)
    shutil.copytree(
        os.path.join(source_dir, "sqlglot"),
        os.path.join(tree, "sqlglot"),
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for file in files:
        path = _part(file, "old") or _part(file, "new")
        if not path or _part(file, "binary") or not path.startswith("sqlglot/"):
            continue
        full = os.path.join(tree, path)
        if _part(file, "old") is None:
            os.makedirs(os.path.dirname(full), exist_ok=True)
            text = ""
        elif not os.path.isfile(full):
            return False
        else:
            with open(full, encoding="utf-8") as handle:
                text = handle.read()
        if _part(file, "new") is None:
            os.remove(full)
            continue
        hunks = _part(file, "hunks")
        written = apply(text, hunks) if hunks else text
        with open(full, "w", encoding="utf-8") as handle:
            handle.write(written)
    return True


def _ensure_import_path():
    """python -S omits a PYTHONPATH entry that does not exist when the process starts."""
    import os

    for entry in reversed([item for item in os.environ.get("PYTHONPATH", "").split(os.pathsep) if item]):
        while entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(1, entry)


def _collect_forked():
    """Fingerprint in a child. This process never imports sqlglot, so the next commit is a fresh import."""
    read_fd, write_fd = os.pipe()
    try:
        pid = os.fork()
    except OSError:
        os.close(read_fd)
        os.close(write_fd)
        raise
    if pid == 0:
        try:
            os.close(read_fd)
            null = os.open(os.devnull, os.O_RDWR)
            os.dup2(null, 0)
            os.dup2(null, 1)
            os.dup2(null, 2)
            if null > 2:
                os.close(null)
            _ensure_import_path()
            importlib.invalidate_caches()
            sys.path_importer_cache.clear()
            data = json.dumps(collect(), separators=(",", ":")).encode()
            view = memoryview(data)
            while view:
                written = os.write(write_fd, view)
                view = view[written:]
            os.close(write_fd)
        except BaseException:
            try:
                os.close(write_fd)
            except OSError:
                pass
            os._exit(1)
        os._exit(0)
    os.close(write_fd)
    chunks = []
    try:
        while True:
            chunk = os.read(read_fd, 1 << 20)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(read_fd)
    _child, status = os.waitpid(pid, 0)
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0 or not chunks:
        return None
    return json.loads(b"".join(chunks))


def _reply(payload):
    try:
        request = json.loads(payload)
        if not prepare_tree(request["source"], request["files"]):
            body = {"ok": False, "prepared": False}
        else:
            try:
                fp = _collect_forked()
            except Exception:
                body = {"ok": False, "prepared": True}
            else:
                body = {"ok": False, "prepared": True} if fp is None else {"ok": True, "prepared": True, "fp": fp}
    except Exception as error:
        body = {"raise": type(error).__name__, "msg": str(error)}
    return json.dumps(body, separators=(",", ":")).encode() + b"\n"


def serve():
    """One JSON request per line, one JSON reply per line, until stdin closes."""
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            break
        sys.stdout.buffer.write(_reply(line))
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    if "--worker" in sys.argv:
        serve()
    else:
        main()
