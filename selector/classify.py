"""The path of a patched file -> the family of rules that decides its tests."""


def classify(path, data_files=()):
    """"source": sqlglot Python code, judged line by line (rules 1-6)
    "data":   a fixture, or any file a test opened during the map build (rule 7)
    "test":   Python under tests/ (rule 8)
    "other":  everything else: packaging, sqlglotc, rust, docs, unknown (rule 9)
    """
    python = path.endswith(".py")
    if python and path.startswith("sqlglot/"):
        return "source"
    if path in data_files or (path.startswith("tests/") and not python):
        return "data"
    if python and path.startswith("tests/"):
        return "test"
    return "other"
