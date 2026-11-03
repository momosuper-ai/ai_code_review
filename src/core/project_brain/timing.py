"""Per-task Project Brain segment timing."""

from contextlib import contextmanager
from contextvars import ContextVar
import math
import threading
import time

SEGMENT_NAMES = (
    "source_catalog",
    "bundle",
    "execute_total",
    "recovery_auth",
    "model_call",
    "checkpoint_commit",
    "claim",
)

_ACCUMULATOR: ContextVar[dict | None] = ContextVar("brain_timing_accumulator", default=None)
_LOCK_KEY = "_lock"


def start() -> None:
    state = {name: {"seconds": 0.0, "count": 0} for name in SEGMENT_NAMES}
    state[_LOCK_KEY] = threading.Lock()
    _ACCUMULATOR.set(state)


def active() -> bool:
    return _ACCUMULATOR.get() is not None


def _state() -> dict:
    state = _ACCUMULATOR.get()
    if state is None:
        start()
        state = _ACCUMULATOR.get()
    return state


def record(name: str, seconds: float) -> None:
    if name not in SEGMENT_NAMES or type(seconds) not in {int, float} or not math.isfinite(seconds):
        raise ValueError("知识阶段计时字段无效")
    state = _state()
    with state[_LOCK_KEY]:
        state[name]["seconds"] += max(0.0, float(seconds))
        state[name]["count"] += 1


@contextmanager
def segment(name: str):
    if name not in SEGMENT_NAMES:
        raise ValueError("知识阶段计时名称无效")
    state = _state()
    started = time.perf_counter()
    try:
        yield
    finally:
        record(name, time.perf_counter() - started)


def snapshot() -> dict[str, dict[str, int | float]]:
    state = _ACCUMULATOR.get()
    if state is None:
        state = {name: {"seconds": 0.0, "count": 0} for name in SEGMENT_NAMES}
        lock = threading.Lock()
    else:
        lock = state[_LOCK_KEY]
    with lock:
        return {
            name: {
                "seconds": round(float(state[name]["seconds"]), 3),
                "count": int(state[name]["count"]),
            }
            for name in SEGMENT_NAMES
        }
