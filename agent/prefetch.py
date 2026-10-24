"""Prefetch: background work for the likely next step (AGENT_PLAN.md section 9, flag `prefetch`).

    pf = Prefetcher(max_workers=4)
    pf.submit(session_id, key, fn)     # fn() -> CachedResult | list[CachedResult] | None
    pf.drain(session_id)               # -> {key: CachedResult} finished so far (removed from the pool)
    pf.wait(session_id, timeout)       # tests: wait until the session's jobs are done

Results never touch a Session object from a worker thread: workers return CachedResult records,
and the engine merges them into `session.results` at the start of the next turn (and before a
search), under the session's own lock. Jobs are keyed by session; a key already queued or done is
not submitted twice. `synchronous=True` runs jobs inline (deterministic tests).
"""
import threading
from concurrent.futures import ThreadPoolExecutor, wait as wait_futures


class Prefetcher:
    def __init__(self, max_workers=4, synchronous=False):
        self.synchronous = synchronous
        self._pool = None if synchronous else ThreadPoolExecutor(max_workers=max_workers,
                                                                  thread_name_prefix="prefetch")
        self._lock = threading.Lock()
        self._jobs = {}      # session_id -> {key: Future | None}
        self._done = {}      # session_id -> {key: CachedResult}
        self._errors = {}    # session_id -> {key: str}
        self._closed = False

    def _store(self, session_id, key, result):
        records = result if isinstance(result, list) else ([result] if result is not None else [])
        with self._lock:
            bucket = self._done.setdefault(session_id, {})
            for rec in records:
                bucket[getattr(rec, "key", key)] = rec

    def _run(self, session_id, key, fn):
        try:
            self._store(session_id, key, fn())
        except Exception as exc:  # a failed prefetch is only a missed optimisation
            with self._lock:
                self._errors.setdefault(session_id, {})[key] = f"{type(exc).__name__}: {exc}"

    def submit(self, session_id, key, fn):
        """Queue fn for the session unless `key` is already queued/done. Returns True if queued."""
        with self._lock:
            if self._closed:
                return False
            jobs = self._jobs.setdefault(session_id, {})
            if key in jobs:
                return False
            jobs[key] = None
        if self.synchronous:
            self._run(session_id, key, fn)
            return True
        future = self._pool.submit(self._run, session_id, key, fn)
        with self._lock:
            self._jobs[session_id][key] = future
        return True

    def pending(self, session_id):
        with self._lock:
            return [k for k, f in self._jobs.get(session_id, {}).items() if f is not None and not f.done()]

    def drain(self, session_id):
        """Finished results for the session (each returned once)."""
        with self._lock:
            return self._done.pop(session_id, {})

    def errors(self, session_id):
        with self._lock:
            return dict(self._errors.get(session_id, {}))

    def wait(self, session_id, timeout=10.0):
        with self._lock:
            futures = [f for f in self._jobs.get(session_id, {}).values() if f is not None]
        if futures:
            wait_futures(futures, timeout=timeout)

    def forget(self, session_id):
        with self._lock:
            self._jobs.pop(session_id, None)
            self._done.pop(session_id, None)
            self._errors.pop(session_id, None)

    def shutdown(self, wait=True):
        with self._lock:
            self._closed = True
        if self._pool is not None:
            self._pool.shutdown(wait=wait, cancel_futures=not wait)
