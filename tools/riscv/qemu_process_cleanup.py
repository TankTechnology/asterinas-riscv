"""Shared deferred termination handling for bounded QEMU subprocesses."""

from __future__ import annotations

import signal
import threading
from contextlib import contextmanager


class _TerminationRequested(BaseException):
    """A process-termination signal requested bounded child cleanup."""

    def __init__(self, signum: int) -> None:
        super().__init__(signum)
        self.signum = signum


class _DeferredTermination:
    """Record fatal signals until code reaches a cleanup-safe checkpoint."""

    def __init__(self) -> None:
        self._pending_signum: int | None = None

    def request(self, signum: int) -> None:
        if self._pending_signum is None:
            self._pending_signum = signum

    def raise_if_pending(self) -> None:
        if self._pending_signum is not None:
            raise _TerminationRequested(self._pending_signum)


@contextmanager
def _defer_termination_until_cleanup():
    """Turn fatal controller signals into an unwind through child cleanup."""

    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("subprocess controllers must run on the main thread")

    termination_signals = (signal.SIGHUP, signal.SIGTERM)
    previous = {signum: signal.getsignal(signum) for signum in termination_signals}
    deferred = _DeferredTermination()

    def request_termination(signum: int, _frame: object) -> None:
        deferred.request(signum)

    active_error: BaseException | None = None
    try:
        for signum in termination_signals:
            signal.signal(signum, request_termination)
        yield deferred
    except BaseException as error:
        active_error = error
        raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if active_error is None:
            deferred.raise_if_pending()
