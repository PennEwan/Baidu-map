"""Response-paced slots; existing algorithm limiter stays unchanged."""
from contextlib import asynccontextmanager
import time

from .stage_ledger import current


class RequestStopped(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@asynccontextmanager
async def request_slot(gate, token, deadline, *, stage="request", attempt=1):
    ledger = current()
    lock_started = time.perf_counter()
    async with gate.attempt_lock:
        if ledger:
            ledger.record("slot_wait", seconds=time.perf_counter() - lock_started)
        if token.cancelled:
            raise RequestStopped("cancelled")
        if not await gate.wait(deadline):
            raise RequestStopped("deadline")
        if token.cancelled:
            raise RequestStopped("cancelled")
        if time.monotonic() >= deadline:
            raise RequestStopped("deadline")
        outcome = {"reason": "interrupted"}
        if ledger:
            ledger.enter_inflight()
        started = time.perf_counter()
        try:
            yield outcome
        finally:
            if ledger:
                ledger.record(stage, seconds=time.perf_counter() - started, reason=outcome.get("reason"), attempt=attempt)
                ledger.exit_inflight()
            gate.completed(outcome["reason"])
