"""Selected tests -> workers. A commit costs its busiest worker plus 2 s per worker (SCORING.md)."""

WORKER_MS = 2000
MAX_WORKERS = 8
EXACT_LIMIT = 12  # at most this many tests: every assignment is searched


def _exact(sizes, workers, limit, floor):
    """The split of `sizes` (longest first) over `workers` whose busiest load is lowest and below
    `limit`: (load, worker of each size), or None. Stops at `floor`, the best any split can do.
    A partial placement whose load already meets the best load is not explored.
    """
    count = len(sizes)
    loads, where = [0] * workers, [0] * count
    best = [limit, None]

    def place(i, used):
        if i == count:
            if max(loads) < best[0]:
                best[0], best[1] = max(loads), where[:]
            return
        size, tried = sizes[i], set()
        previous = where[i - 1] if i and size == sizes[i - 1] else -1
        for worker in range(min(used + 1, workers)):  # one empty worker is as good as another
            if worker < previous:  # equal tests stay in worker order, so each split is tried once
                continue
            load = loads[worker]
            if load + size >= best[0] or load in tried:
                continue
            tried.add(load)
            loads[worker] = load + size
            where[i] = worker
            place(i + 1, max(used, worker + 1))
            loads[worker] = load
            if best[0] <= floor:
                return

    place(0, 0)
    return (best[0], best[1]) if best[1] else None


def _first_of_each_duration(items, durations):
    """(index, test, ms) of the first test of each duration, in list order.

    Two tests of equal length move the same load, and a later one cannot beat an earlier swap.
    """
    seen, found = set(), []
    for index, test in enumerate(items):
        ms = durations[test]
        if ms in seen:
            continue
        seen.add(ms)
        found.append((index, test, ms))
    return found


def _swap_pass(workers, loads, durations):
    """Swap one test on the busiest worker with one on each other worker, once, when that lowers the max."""
    if len(workers) < 2:
        return
    busiest = max(range(len(loads)), key=lambda worker: loads[worker])
    for other in range(len(workers)):
        if other == busiest:
            continue
        current = max(loads)
        rest = max((loads[worker] for worker in range(len(loads)) if worker != busiest and worker != other), default=0)
        choice = None
        for i, _left, left_ms in _first_of_each_duration(workers[busiest], durations):
            for j, _right, right_ms in _first_of_each_duration(workers[other], durations):
                if left_ms == right_ms:
                    continue
                new_busy = loads[busiest] - left_ms + right_ms
                new_other = loads[other] - right_ms + left_ms
                new_max = new_busy if new_busy >= new_other else new_other
                if rest > new_max:
                    new_max = rest
                if new_max < current and (choice is None or new_max < choice[0]):
                    choice = (new_max, i, j)
        if choice is None:
            continue
        _, i, j = choice
        left, right = workers[busiest][i], workers[other][j]
        workers[busiest][i], workers[other][j] = right, left
        loads[busiest] = loads[busiest] - durations[left] + durations[right]
        loads[other] = loads[other] - durations[right] + durations[left]


def pack(tests, durations):
    """-> [[test numbers], ...], no empty worker; [] when nothing is selected.

    For every worker count 1..8: longest test first onto the least loaded worker. Up to 12 tests,
    every assignment is searched and kept when it beats that. Larger selections then swap one test
    off the busiest worker with each other worker, once, when the swap lowers the max load.
    The cheapest result wins, fewer workers on a tie.
    """
    order = sorted(tests, key=lambda test: (-durations[test], test))
    if not order:
        return []
    counts = range(1, min(MAX_WORKERS, len(order)) + 1)
    best_cost, best = None, None
    improve = len(order) > EXACT_LIMIT
    for count in counts:
        workers, loads = [[] for _ in range(count)], [0] * count
        for test in order:
            lightest = loads.index(min(loads))
            workers[lightest].append(test)
            loads[lightest] += durations[test]
        if improve:
            _swap_pass(workers, loads, durations)
        cost = max(loads) + WORKER_MS * count
        if best is None or cost < best_cost:
            best_cost, best = cost, workers
    if len(order) <= EXACT_LIMIT:
        sizes = [durations[test] for test in order]
        total = sum(sizes)
        for count in counts:
            floor = max(sizes[0], -(-total // count))
            limit = best_cost - WORKER_MS * count  # the busiest load must be below this to win
            found = _exact(sizes, count, limit, floor) if floor < limit else None
            if found:
                best_cost = found[0] + WORKER_MS * count
                best = [[] for _ in range(count)]
                for test, worker in zip(order, found[1]):
                    best[worker].append(test)
    return [worker for worker in best if worker]
