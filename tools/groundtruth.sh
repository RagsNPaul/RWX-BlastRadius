#!/usr/bin/env bash
# Real failing set for every bank/<id>.patch that has no bank/<id>.json yet.
# The suite runs in the pinned image, with requirements.lock and real process pools.
#
#   bash tools/groundtruth.sh                       # bank/*.patch -> bank/<id>.json
#   bash tools/groundtruth.sh --validate 3utz rszg 9u5k
#   GT_PLATFORM=linux/amd64 bash tools/groundtruth.sh
#   GT_JOBS=1 bash tools/groundtruth.sh             # default. Two suites at once got xdist workers killed.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
SHA=eb11aec4be8cb2122afe713a4816662c3f0fb2fa
IMAGE=blast-radius-gt
PLATFORM=${GT_PLATFORM:-linux/arm64}
JOBS=${GT_JOBS:-1}
TESTS_JSON=$ROOT/sample/tests.json

if ! docker info >/dev/null 2>&1; then
  echo "groundtruth: the Docker daemon is not running" >&2
  exit 1
fi

# Build from a directory that holds only the lock file, so the sqlglot checkout is not uploaded.
stage=${TMPDIR:-/tmp}/blast-gt-docker
mkdir -p "$stage"
cp "$ROOT/requirements.lock" "$stage/requirements.lock"
echo "groundtruth: building $IMAGE ($PLATFORM)" >&2
docker build --platform "$PLATFORM" -t "$IMAGE" -f - "$stage" <<'EOF'
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --retries 15 --resume-retries 15 --timeout 120 \
    -r /tmp/requirements.lock "coverage==7.17.4"
EOF

# One patch: pristine archive on stdin, pytest log and the container's exit code on the host.
run_one() {
  local id="$1" patch="$2" log="$3" rcfile="$4"
  local rc=0
  mkdir -p "$(dirname "$log")"
  git -C sqlglot archive --format=tar "$SHA" \
    | docker run --rm -i --init --network none --platform "$PLATFORM" \
        -e SKIP_INTEGRATION=0 -e COLUMNS=400 \
        -v "$patch":/patch:ro "$IMAGE" \
        sh -c 'mkdir -p /work && tar -xf - -C /work && cd /work && git apply --whitespace=nowarn /patch || { echo GROUNDTRUTH_APPLY_FAILED; exit 2; }
          PYTHONPATH=. python -m pytest tests -n auto -rA -p no:cacheprovider --timeout=300 --continue-on-collection-errors' \
        >"$log" 2>&1 || rc=$?
  echo "$rc" >"$rcfile"
  echo "groundtruth: $id container exit $rc" >&2
}

# Log + exit code -> {"failing": [numbers]}. Refuses to write on a short or inconsistent run.
parse_one() {
  local id="$1" log="$2" rcfile="$3" out="$4"
  python3 - "$id" "$log" "$(cat "$rcfile")" "$out" "$TESTS_JSON" <<'PY'
import json, re, sys

commit, log_path, rc_text, out_path, tests_path = sys.argv[1:6]
rc = int(rc_text)
log = open(log_path, errors="replace").read()
tests = json.load(open(tests_path))["tests"]
by_id = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])
fail_status = {"FAILED", "ERROR", "SUBFAILED"}
ok_status = fail_status | {"PASSED", "SKIPPED", "XFAIL", "XPASS", "SUBPASSED"}

def die(message):
    print(f"groundtruth: {commit}: {message}", file=sys.stderr)
    sys.exit(1)

if "GROUNDTRUTH_APPLY_FAILED" in log:
    die("git apply failed")
if rc not in (0, 1):
    die(f"pytest exit {rc} (expected 0 or 1); log {log_path}")

# A subtest summary is `SUBFAILED[title] nodeid` or `SUBFAILED(title) nodeid`, and the title
# can be a multi-line SQL string, so the record continues onto the following lines.
head = re.compile(r"^(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS|SUBPASSED|SUBFAILED)(?:[\[(]|\s)")
node_id = re.compile(r"tests/[A-Za-z0-9_./]+(?:::[A-Za-z0-9_]+)+")
records, current, in_summary = [], None, False
for line in log.splitlines():
    stripped = line.strip()
    if re.match(r"=+ short test summary info =+$", stripped):
        in_summary = True
        continue
    if not in_summary or not stripped:
        continue
    if stripped.startswith("=") and " in " in stripped:
        break
    if head.match(stripped):
        if current:
            records.append(current)
        current = stripped
    elif current is not None:
        current += " " + stripped
if current:
    records.append(current)
if not records:
    die(f"no short test summary in {log_path}")
if any("crashed while running" in record for record in records):
    die("a pytest worker crashed; the run does not say which tests really failed")

seen, failing, unknown = set(), set(), []
for record in records:
    status = record.split("[", 1)[0].split("(", 1)[0].split(" ", 1)[0]
    if status not in ok_status:
        continue
    found = [node for node in node_id.findall(record) if node in by_id]
    if found:
        test_id = found[-1]
        seen.add(test_id)
        if status in fail_status:
            failing.add(by_id[test_id])
        continue
    path = record[len(status):].strip()
    if path.startswith("collecting "):
        path = path[len("collecting "):].strip()
    path = path.split(" - ", 1)[0].strip()
    if status == "ERROR" and "::" not in path and path in by_file:
        seen.update(t["id"] for t in tests if t["file"] == path)
        failing.update(by_file[path])
        continue
    if status in fail_status:
        unknown.append(path[:160])

if unknown:
    die("failing id is not in tests.json: " + ", ".join(unknown[:5]))

missing = [t for t in tests if t["id"] not in seen]
if missing:
    die(f"{len(missing)} numbered tests have no status line, e.g. {missing[0]['id']}")

if (rc == 0 and failing) or (rc == 1 and not failing):
    die(f"pytest exit {rc} but {len(failing)} failing tests were parsed")

numbers = sorted(failing)
with open(out_path, "w") as f:
    json.dump({"failing": numbers}, f, separators=(",", ":"))
    f.write("\n")
print(f"groundtruth: {commit} failing {len(numbers)} -> {out_path}", file=sys.stderr)
PY
}

launch() { # id patch log rcfile outjson  -- remembers jobs in the global lists
  local id="$1"
  run_one "$id" "$2" "$3" "$4" &
  pids+=("$!")
  jobs_id+=("$id")
  jobs_log+=("$3")
  jobs_rc+=("$4")
  jobs_out+=("$5")
  if [ "${#pids[@]}" -ge "$JOBS" ]; then
    wait "${pids[0]}" || true
    pids=("${pids[@]:1}")
  fi
}

finish() {
  local pid
  for pid in "${pids[@]+"${pids[@]}"}"; do
    wait "$pid" || true
  done
  local i
  for i in "${!jobs_id[@]}"; do
    parse_one "${jobs_id[$i]}" "${jobs_log[$i]}" "${jobs_rc[$i]}" "${jobs_out[$i]}"
  done
}

pids=()
jobs_id=()
jobs_log=()
jobs_rc=()
jobs_out=()

if [ "${1:-}" = "--validate" ]; then
  shift
  ids=("$@")
  if [ "${#ids[@]}" -eq 0 ]; then
    ids=(3utz rszg 9u5k)
  fi
  out=${TMPDIR:-/tmp}/blast-gt
  mkdir -p "$out"
  for id in "${ids[@]}"; do
    patch=$ROOT/sample/commits/$id.patch
    if [ ! -f "$patch" ]; then
      echo "groundtruth: no sample patch $id" >&2
      exit 1
    fi
    launch "$id" "$patch" "$out/$id.log" "$out/$id.rc" "$out/$id.json"
  done
  finish
  python3 - "$out" "$ROOT/sample/answers.json" "${ids[@]}" <<'PY'
import json, sys
out, answers_path, *ids = sys.argv[1:]
answers = json.load(open(answers_path))["commits"]
bad = 0
for commit in ids:
    got = json.load(open(f"{out}/{commit}.json"))["failing"]
    expected = answers[commit]["failing"]
    if got != expected:
        bad += 1
        print(f"MISMATCH {commit}: got {got} expected {expected}", file=sys.stderr)
    else:
        print(f"match {commit}: {len(got)} failing", file=sys.stderr)
sys.exit(1 if bad else 0)
PY
  echo "groundtruth: validate ok (${ids[*]})" >&2
  exit 0
fi

shopt -s nullglob
patches=("$ROOT"/bank/*.patch)
if [ "${#patches[@]}" -eq 0 ]; then
  echo "groundtruth: bank/ has no patches" >&2
  exit 1
fi
mkdir -p "$ROOT/bank/logs"
ran=0
for patch in "${patches[@]}"; do
  id=$(basename "$patch" .patch)
  if [ -f "$ROOT/bank/$id.json" ]; then
    continue
  fi
  launch "$id" "$patch" "$ROOT/bank/logs/$id.log" "$ROOT/bank/logs/$id.rc" "$ROOT/bank/$id.json"
  ran=$((ran + 1))
done
if [ "$ran" -eq 0 ]; then
  echo "groundtruth: every patch already has a json" >&2
  exit 0
fi
finish
echo "groundtruth: wrote $ran json files" >&2
