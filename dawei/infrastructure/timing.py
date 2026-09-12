"""Optional audit timing recorder (no response content, default off)."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar


class AuditTiming:
    """Thread-safe durations, counters and scalar values for one audit scope."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._durations: dict[str, float] = {}
        self._counters: dict[str, int] = {}
        self._values: dict[str, object] = {}

    def add(self, stage: str, seconds: float) -> None:
        with self._lock:
            self._durations[stage] = self._durations.get(stage, 0.0) + float(seconds)

    def incr(self, counter: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[counter] = self._counters.get(counter, 0) + int(amount)

    def set(self, key: str, value: object) -> None:
        with self._lock:
            self._values[key] = value

    def snapshot(self) -> dict[str, dict]:
        with self._lock:
            return {
                "durations": dict(self._durations),
                "counters": dict(self._counters),
                "values": dict(self._values),
            }


_CURRENT: ContextVar[AuditTiming | None] = ContextVar("dawei_audit_timing", default=None)


def current_timing() -> AuditTiming | None:
    return _CURRENT.get()


@contextmanager
def timing_scope(recorder: AuditTiming | None) -> Iterator[AuditTiming | None]:
    if recorder is None:
        yield None
        return
    token = _CURRENT.set(recorder)
    try:
        yield recorder
    finally:
        _CURRENT.reset(token)


@contextmanager
def measure(stage: str) -> Iterator[None]:
    """Record a duration into the ambient recorder, no-op when timing is off."""
    recorder = _CURRENT.get()
    started = time.perf_counter()
    try:
        yield
    finally:
        if recorder is not None:
            recorder.add(stage, time.perf_counter() - started)


@contextmanager
def scoped(recorder: AuditTiming | None, stage: str) -> Iterator[None]:
    """Record a duration into an explicit recorder (usable on owner threads)."""
    started = time.perf_counter()
    try:
        yield
    finally:
        if recorder is not None:
            recorder.add(stage, time.perf_counter() - started)
