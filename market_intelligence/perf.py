"""Optional server-side timings. Silent unless ``MI_PERF`` is 1, true, or yes."""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from typing import Iterator


def enabled() -> bool:
    return os.environ.get("MI_PERF", "").strip().lower() in {"1", "true", "yes"}


@contextmanager
def span(name: str) -> Iterator[None]:
    if not enabled():
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        print("mi_perf {0} {1:.1f}ms".format(name, elapsed_ms), file=sys.stderr)
