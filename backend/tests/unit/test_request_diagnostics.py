import logging
import threading
import time
import unittest
from unittest.mock import patch

from verifeye.request_diagnostics import RequestDiagnostics


class RequestDiagnosticsTests(unittest.TestCase):
    def test_completed_request_is_logged_without_a_stack_dump(self):
        diagnostics = RequestDiagnostics(slow_request_seconds=1)
        with self.assertLogs("uvicorn.error.verifeye.requests", logging.INFO) as captured, \
                patch("verifeye.request_diagnostics.faulthandler.dump_traceback") as dump:
            request, timer = diagnostics.begin("GET", "/api/cameras")
            diagnostics.finish(request, timer, 200)
            time.sleep(.02)
        dump.assert_not_called()
        output = "\n".join(captured.output)
        self.assertIn("request.started", output)
        self.assertIn("request.completed", output)
        self.assertIn("status=200", output)

    def test_slow_request_logs_and_dumps_all_threads(self):
        diagnostics = RequestDiagnostics(slow_request_seconds=.01)
        dumped = threading.Event()

        def record_dump(**_kwargs):
            dumped.set()

        with self.assertLogs("uvicorn.error.verifeye.requests", logging.WARNING) as captured, \
                patch("verifeye.request_diagnostics.faulthandler.dump_traceback", side_effect=record_dump) as dump:
            request, timer = diagnostics.begin("POST", "/api/onvif/discover")
            self.assertTrue(dumped.wait(1))
            diagnostics.finish(request, timer, 200)
        dump.assert_called_once()
        self.assertTrue(dump.call_args.kwargs["all_threads"])
        self.assertIn("request.slow", "\n".join(captured.output))
        self.assertIn("POST /api/onvif/discover", "\n".join(captured.output))

    def test_zero_threshold_disables_stack_dump_timer(self):
        diagnostics = RequestDiagnostics(slow_request_seconds=0)
        request, timer = diagnostics.begin("GET", "/")
        try:
            self.assertIsNone(timer)
        finally:
            diagnostics.finish(request, timer, 200)


if __name__ == "__main__":
    unittest.main()
