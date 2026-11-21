"""Selected tests -> workers. A commit costs its busiest worker plus 2 s per worker (SCORING.md)."""

WORKER_MS = 2000
MAX_WORKERS = 8
EXACT_LIMIT = 12  # with at most this many tests the best split is searched for
NODE_BUDGET = 20000  # search steps per worker count, so an unlucky input cannot burn CPU


def _exact(sizes, workers, limit, floor):
    """The split of `sizes` (longest first) over `workers` whose busiest load is lowest and below
    `limit`: (load, worker of each size), or None. Stops at `floor`, the best any split can do.
    """
    count = len(sizes)
    loads, where = [0] * workers, [0] * count
    best, steps = [limit, None], 0

    def place(i, used):
        nonlocal steps
        if i == count:
            if max(loads) < best[0]:
                best[0], best[1] = max(loads), where[:]
            return
        steps += 1
        size, tried = sizes[i], set()
        for worker in range(min(used + 1, workers)):  # one empty worker is as good as another
            load = loads[worker]
            if load + size >= best[0] or load in tried:
                continue
            tried.add(load)
            loads[worker] = load + size
            where[i] = worker
            place(i + 1, max(used, worker + 1))
            loads[worker] = load
            if best[0] <= floor or steps > NODE_BUDGET:
                return

    place(0, 0)
    return (best[0], best[1]) if best[1] else None


def pack(tests, durations):
    """-> [[test numbers], ...], no empty worker; [] when nothing is selected.

    For every worker count 1..8: longest test first onto the least loaded worker. Small
    selections are also searched exactly. The cheapest result wins, fewer workers on a tie.
    """
    order = sorted(tests, key=lambda test: (-durations[test], test))
    if not order:
        return []
    counts = range(1, min(MAX_WORKERS, len(order)) + 1)
    best_cost, best = None, None
    for count in counts:
        workers, loads = [[] for _ in range(count)], [0] * count
        for test in order:
            lightest = loads.index(min(loads))
            workers[lightest].append(test)
            loads[lightest] += durations[test]
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
