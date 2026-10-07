"""Cooperative deadlines for reads, scoped to one caller and reset on exit."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import time

from noetrail.errors import NoetrailError


class ReadTimeout(NoetrailError):
    code = "read_timeout"


_deadline: ContextVar[float | None] = ContextVar("read_deadline", default=None)


def check_deadline() -> None:
    end = _deadline.get()
    if end is not None and time.monotonic() >= end:
        raise ReadTimeout("Read deadline exceeded; narrow the query and retry.")


@contextmanager
def read_deadline(seconds: float) -> Iterator[None]:
    end = time.monotonic() + seconds
    parent = _deadline.get()
    token = _deadline.set(min(parent, end) if parent is not None else end)
    try:
        check_deadline()
        yield
        check_deadline()
    finally:
        _deadline.reset(token)
