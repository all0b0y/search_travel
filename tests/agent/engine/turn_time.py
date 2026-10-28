"""Turn time bounds that do not depend on other load on the machine (#14).

A bound like `TURN_MS_MAX` means «an in-process turn without a model takes at most so long on an unloaded dev
machine». Wall clock alone also counts the time the OS gave the CPU to other processes: at load average 8–11 a
~1 250 ms turn read 1 560–2 100 ms. Process CPU time is no fix either: the parallel tool calls make it larger than
the wall clock (1 500 ms of CPU for the same turn), and it would miss a turn that waits.

So the turn is timed by the wall clock and calibrated by a fixed reference operation timed on the same machine
right before and after it: when the reference runs slower than on the unloaded dev machine (REFERENCE_MS), the
turn's time is divided by that slowdown. The reference is shaped like a turn: Python work under the GIL plus
sqlite queries in parallel threads (sqlite releases the GIL, as the parallel tool calls do), because under load
the parallel part loses its speedup and slows down more than the Python part. On an unloaded machine as fast as
the dev one (or faster) the factor is 1 and the bound is the plain wall clock.
"""
import sqlite3
import threading
import time

REFERENCE_MS = 37.0   # _reference() on an unloaded dev machine: median 37.2 ms, min 36.0 ms (Apple M-series, 10 cores)
_QUERY = "with recursive c(x) as (select 1 union all select x + 1 from c where x < 300000) select sum(x % 7) from c"


def _query():
    con = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        con.execute(_QUERY).fetchone()
    finally:
        con.close()


def _reference():
    t0 = time.perf_counter()
    acc, d = 0, {}
    for i in range(120000):
        k = f"k{i % 500}"
        d[k] = d.get(k, 0) + i
        acc += len(k)
    threads = [threading.Thread(target=_query) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return (time.perf_counter() - t0) * 1000


def slowdown():
    """How much slower than the unloaded dev machine this one runs right now (≥ 1)."""
    return max(1.0, sorted(_reference() for _ in range(3))[1] / REFERENCE_MS)


def calibrated(fn, *args, **kwargs):
    """(fn's result, its wall ms divided by the machine's slowdown around the call)."""
    before = slowdown()
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    wall = (time.perf_counter() - t0) * 1000
    return result, round(wall / ((before + slowdown()) / 2), 1)
