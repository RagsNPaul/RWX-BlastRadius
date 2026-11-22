"""Blast Radius selector: for every patch in <data>/commits, the tests to run and their workers.

stdout carries one answer line per commit and nothing else; logs go to stderr.

    python -S /app/selector/main.py                  # the image: /data, /opt/sqlglot, /app/maps
    python3 selector/main.py --data sample           # locally: ./sqlglot and ./maps
    python3 selector/main.py --commits bank --tests sample/tests.json
"""
import json
import os
import resource
import sys

from pack import pack
from patch import parse
from rules import Maps, close_fingerprint_worker, select

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def log(*message):
    print(*message, file=sys.stderr)


def option(name, default):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv[1:-1] else default


def main():
    data = option("--data", "/data")
    pinned = "/opt/sqlglot" if os.path.isdir("/opt/sqlglot/sqlglot") else os.path.join(REPO, "sqlglot")
    source, maps_dir = option("--src", pinned), option("--maps", os.path.join(REPO, "maps"))
    commits = option("--commits", os.path.join(data, "commits"))
    tests_path = option("--tests", os.path.join(data, "tests.json"))

    with open(tests_path) as f:
        tests = json.load(f)["tests"]
    durations = [0] * len(tests)
    for test in tests:
        durations[test["number"]] = int(test["seconds"] * 1000 + 0.5)  # score.py's rounding
    everything = frozenset(range(len(tests)))

    try:
        maps = Maps(maps_dir, source)
        maps.check(tests)
    except Exception as error:  # without usable maps every patch gets the full suite
        maps = None
        log(f"maps unusable, selecting every test: {error!r}")

    packed, answers = {}, []
    for name in sorted(os.listdir(commits)):
        if not name.endswith(".patch"):
            continue
        commit = name[: -len(".patch")]
        try:
            if maps is None:
                raise RuntimeError("no maps")
            with open(os.path.join(commits, name), "rb") as f:
                text = f.read().decode("utf-8")
            files = parse(text)
            if text.strip() and not files:
                raise ValueError("no file found in the patch")
            selected, reasons = select(files, maps)
            selected = frozenset(selected)
        except Exception as error:  # never let one patch end the run: it gets the full suite
            selected, reasons = everything, [f"all:error {error!r}"]
        if selected not in packed:
            try:
                packed[selected] = pack(selected, durations)
            except Exception as error:
                packed[selected] = [sorted(selected)] if selected else []
                reasons.append(f"pack error {error!r}")
        workers = packed[selected]
        answers.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
        cost = max(sum(durations[t] for t in w) for w in workers) + 2000 * len(workers) if workers else 0
        log(commit, len(selected), "tests", f"{cost / 1000:.3f}s", ",".join(reasons))

    if answers:
        sys.stdout.write("\n".join(answers) + "\n")
    sys.stdout.flush()
    close_fingerprint_worker()
    used = [resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    log(f"cpu_seconds {sum(u.ru_utime + u.ru_stime for u in used):.3f}")


if __name__ == "__main__":
    main()
