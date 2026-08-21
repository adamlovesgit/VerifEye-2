"""Request timing and on-demand thread dumps for diagnosing server hangs."""

from __future__ import annotations

from dataclasses import dataclass
import faulthandler
import logging
import os
import sys
import threading
import time
import uuid


# Uvicorn configures this logger in normal server runs, so INFO request lifecycle
# records are visible without requiring a separate application logging setup.
logger = logging.getLogger("uvicorn.error.verifeye.requests")


@dataclass(frozen=True)
class ActiveRequest:
    request_id: str
    method: str
    path: str
    started_at: float


class RequestDiagnostics:
    """Track active requests and dump all Python stacks when one runs too long."""

    def __init__(self, slow_request_seconds: float | None = None):
        if slow_request_seconds is None:
            slow_request_seconds = float(os.getenv("VERIFEYE_SLOW_REQUEST_SECONDS", "10"))
        self.slow_request_seconds = slow_request_seconds
        self._active: dict[str, ActiveRequest] = {}
        self._lock = threading.Lock()

    def begin(self, method: str, path: str) -> tuple[ActiveRequest, threading.Timer | None]:
        request = ActiveRequest(uuid.uuid4().hex[:12], method, path, time.monotonic())
        with self._lock:
            self._active[request.request_id] = request
        logger.info(
            "request.started id=%s method=%s path=%s",
            request.request_id, request.method, request.path,
        )
        timer = None
        if self.slow_request_seconds > 0:
            timer = threading.Timer(
                self.slow_request_seconds, self._report_slow, args=(request.request_id,),
            )
            timer.daemon = True
            timer.start()
        return request, timer

    def finish(
        self,
        request: ActiveRequest,
        timer: threading.Timer | None,
        status_code: int | None,
        failed: bool = False,
    ) -> None:
        if timer is not None:
            timer.cancel()
        with self._lock:
            self._active.pop(request.request_id, None)
        elapsed_ms = (time.monotonic() - request.started_at) * 1000
        logger.info(
            "request.%s id=%s method=%s path=%s status=%s elapsed_ms=%.1f",
            "failed" if failed else "completed",
            request.request_id, request.method, request.path,
            status_code if status_code is not None else "exception", elapsed_ms,
        )

    def _report_slow(self, request_id: str) -> None:
        with self._lock:
            request = self._active.get(request_id)
            active = tuple(self._active.values())
        if request is None:
            return
        elapsed = time.monotonic() - request.started_at
        active_summary = ", ".join(
            f"{item.request_id}:{item.method} {item.path}"
            for item in sorted(active, key=lambda item: item.started_at)
        )
        logger.warning(
            "request.slow id=%s method=%s path=%s elapsed_seconds=%.1f active_requests=[%s]; "
            "dumping all Python thread stacks",
            request.request_id, request.method, request.path, elapsed, active_summary,
        )
        try:
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        except (AttributeError, OSError, RuntimeError, ValueError):
            logger.exception("Unable to dump Python thread stacks for request id=%s", request_id)
