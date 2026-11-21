"""Import fingerprint of a sqlglot tree.

Run with `python -S` and the package on PYTHONPATH. Prints one JSON object,
key to sha256, and nothing else. The selector compares it with maps/import_fp.json.
"""
import enum
import hashlib
import importlib
import json
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


if __name__ == "__main__":
    main()
