"""Build maps/enums.json from the pinned commit.

Each sqlglot module records the local names that refer to an Enum subclass whose
equality is still identity. IntEnum, str mixins, and any class that defines __eq__
are listed, and their names are left out, because `is` and `==` differ for them.
The selector reads this file and does not import sqlglot.
"""
import ast
import json
import os
import sys

STDLIB_ENUMS = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag", "ReprEnum"}
# Bases whose __eq__ is not identity. IntEnum and friends compare by value.
VALUE_EQ = {"int", "str", "float", "bytes", "IntEnum", "StrEnum", "Flag", "IntFlag", "ReprEnum"}


def module_of(package, path):
    relative = os.path.relpath(path, package)
    name = relative.replace(os.sep, ".")
    if name.endswith(".__init__.py"):
        return name[: -len(".__init__.py")]
    return name[: -len(".py")]


def relative_module(current, level, mod, is_package):
    parts = current.split(".")
    if not is_package:
        parts = parts[:-1]
    if level > 1:
        parts = parts[: 1 - level]
    if mod:
        parts.extend(mod.split("."))
    return ".".join(parts)


def collect(package):
    modules = {}
    for folder, _, names in os.walk(os.path.join(package, "sqlglot")):
        for name in names:
            if not name.endswith(".py"):
                continue
            path = os.path.join(folder, name)
            module = module_of(package, path)
            source = os.path.relpath(path, package)
            with open(path, encoding="utf-8", errors="replace") as handle:
                try:
                    tree = ast.parse(handle.read())
                except SyntaxError:
                    continue
            classes, imports, assigned = {}, [], set()
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    classes[node.name] = node
                elif isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            assigned.add(target.id)
                elif isinstance(node, (ast.Import, ast.ImportFrom)):
                    imports.append(node)
            modules[module] = {
                "source": source,
                "classes": classes,
                "imports": imports,
                "assigned": assigned,
                "package": name == "__init__.py",
            }
    return modules


def binding(modules, module, name, seen):
    """What `name` refers to in `module`: ('class', mod, cls), ('stdlib', name), or None."""
    if (module, name) in seen:
        return None
    seen.add((module, name))
    info = modules.get(module)
    if info is None:
        return None
    if name in info["classes"]:
        return ("class", module, name)
    if name in {"int", "str", "float", "bytes"}:
        return ("stdlib", name)
    for node in info["imports"]:
        if isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound == name and alias.name == "enum":
                    return ("module", "enum")
            continue
        imported = (
            relative_module(module, node.level, node.module, info["package"])
            if node.level
            else node.module
        )
        for alias in node.names:
            bound = alias.asname or alias.name
            if bound != name or alias.name == "*":
                continue
            if imported == "enum" and alias.name in STDLIB_ENUMS | {"auto"}:
                return ("stdlib", alias.name)
            if imported == "enum":
                return ("module", "enum")
            return binding(modules, imported, alias.name, seen)
    return None


def base_kind(modules, module, expr):
    if isinstance(expr, ast.Name):
        return binding(modules, module, expr.id, set())
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
        found = binding(modules, module, expr.value.id, set())
        if found == ("module", "enum") and expr.attr in STDLIB_ENUMS:
            return ("stdlib", expr.attr)
    return None


def defines_eq(node):
    return any(isinstance(stmt, ast.FunctionDef) and stmt.name == "__eq__" for stmt in node.body)


def classify(modules):
    """module:Class -> whether equality is not identity."""
    pending = []
    for module, info in modules.items():
        for name, node in info["classes"].items():
            bases = [base_kind(modules, module, base) for base in node.bases]
            pending.append((module, name, node, bases))
    enums = {}
    changed = True
    while changed:
        changed = False
        for module, name, node, bases in pending:
            key = f"{module}:{name}"
            if key in enums:
                continue
            enum_base = False
            value_eq = defines_eq(node)
            for kind in bases:
                if kind is None:
                    continue
                if kind[0] == "stdlib" and kind[1] in STDLIB_ENUMS:
                    enum_base = True
                    if kind[1] in VALUE_EQ:
                        value_eq = True
                elif kind[0] == "stdlib" and kind[1] in VALUE_EQ:
                    value_eq = True
                elif kind[0] == "class":
                    parent = f"{kind[1]}:{kind[2]}"
                    if parent in enums:
                        enum_base = True
                        value_eq = value_eq or enums[parent]
            if enum_base:
                enums[key] = value_eq
                changed = True
    return enums


def bound_to(modules, wanted):
    """Per source file, local names that refer to one of the (module, class) pairs in `wanted`."""
    names = {}
    for module, info in modules.items():
        found = []
        for node in info["imports"]:
            if not isinstance(node, ast.ImportFrom):
                continue
            imported = (
                relative_module(module, node.level, node.module, info["package"])
                if node.level
                else node.module
            )
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                if bound in info["assigned"]:
                    continue
                target = binding(modules, imported, alias.name, set())
                if target and target[0] == "class" and (target[1], target[2]) in wanted:
                    found.append(bound)
        for name in info["classes"]:
            if (module, name) in wanted and name not in info["assigned"]:
                found.append(name)
        if found:
            names[info["source"]] = sorted(set(found))
    return names


def safe_names(modules, enums):
    names = {}
    for module, info in modules.items():
        found = []
        for node in info["imports"]:
            if not isinstance(node, ast.ImportFrom):
                continue
            info = modules[module]
            imported = (
                relative_module(module, node.level, node.module, info["package"])
                if node.level
                else node.module
            )
            for alias in node.names:
                if alias.name == "*":
                    continue
                bound = alias.asname or alias.name
                if bound in info["assigned"]:
                    continue
                target = binding(modules, imported, alias.name, set())
                if target and target[0] == "class":
                    key = f"{target[1]}:{target[2]}"
                    if key in enums and not enums[key]:
                        found.append(bound)
        for name, node in info["classes"].items():
            key = f"{module}:{name}"
            if key in enums and not enums[key] and name not in info["assigned"]:
                found.append(name)
        if found:
            names[info["source"]] = sorted(set(found))
    return names


def main():
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    modules = collect(os.path.join(repo, "sqlglot"))
    enums = classify(modules)
    payload = {
        "any_overrides_eq": any(enums.values()),
        "classes": {key: {"eq": value} for key, value in sorted(enums.items())},
        "names": safe_names(modules, enums),
        "scalars": bound_to(
            modules,
            {("sqlglot.tokenizer_core", "Token"), ("sqlglot.tokenizer_core", "TokenType")},
        ),
    }
    dest = os.path.join(repo, "maps", "enums.json")
    with open(dest, "w") as handle:
        json.dump(payload, handle, indent=1)
        handle.write("\n")
    safe = sum(len(values) for values in payload["names"].values())
    print(f"{len(enums)} enums, any_overrides_eq={payload['any_overrides_eq']}, {safe} safe names -> {dest}")
    for key, info in payload["classes"].items():
        print(f"  {'eq' if info['eq'] else 'identity':<8} {key}")


if __name__ == "__main__":
    sys.exit(main())
