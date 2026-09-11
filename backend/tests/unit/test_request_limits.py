"""Unit tests for the streaming upload-envelope limit."""

import asyncio
import sys
from pathlib import Path
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from verifeye.request_limits import UploadRequestBodyLimitMiddleware  # noqa: E402


class UploadRequestBodyLimitTests(unittest.TestCase):
    def run_middleware(self, scope, messages, max_bytes=10):
        invoked = []
        sent = []

        async def receive():
            return messages.pop(0)

        async def send(message):
            sent.append(message)

        async def downstream(_scope, downstream_receive, downstream_send):
            invoked.append(True)
            while True:
                message = await downstream_receive()
                if message["type"] != "http.request" or not message.get("more_body"):
                    break
            await downstream_send({"type": "http.response.start", "status": 204, "headers": []})
            await downstream_send({"type": "http.response.body", "body": b""})

        asyncio.run(UploadRequestBodyLimitMiddleware(downstream, max_bytes)(scope, receive, send))
        return invoked, sent

    def upload_scope(self, path="/api/enroll", headers=()):
        return {"type": "http", "method": "POST", "path": path, "headers": list(headers)}

    def test_declared_oversize_is_rejected_without_calling_downstream(self):
        invoked, sent = self.run_middleware(
            self.upload_scope(headers=[(b"content-length", b"11")]), [],
        )
        self.assertEqual(invoked, [])
        self.assertEqual([message["type"] for message in sent], ["http.response.start", "http.response.body"])
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(sent[1]["body"], b'{"detail":"Upload request body is too large."}')

    def test_streamed_oversize_is_rejected_when_content_length_is_missing(self):
        invoked, sent = self.run_middleware(
            self.upload_scope(),
            [{"type": "http.request", "body": b"12345", "more_body": True},
             {"type": "http.request", "body": b"678901", "more_body": False}],
        )
        self.assertEqual(invoked, [True])
        self.assertEqual([message["status"] for message in sent if message["type"] == "http.response.start"], [413])

    def test_misleading_content_length_does_not_bypass_streaming_limit(self):
        invoked, sent = self.run_middleware(
            self.upload_scope(path="/api/cameras/42/events", headers=[(b"content-length", b"2")]),
            [{"type": "http.request", "body": b"12345678901", "more_body": False}],
        )
        self.assertEqual(invoked, [True])
        self.assertEqual([message["status"] for message in sent if message["type"] == "http.response.start"], [413])

    def test_invalid_content_length_does_not_bypass_streaming_limit(self):
        invoked, sent = self.run_middleware(
            self.upload_scope(headers=[(b"content-length", b"not-a-number")]),
            [{"type": "http.request", "body": b"12345678901", "more_body": False}],
        )
        self.assertEqual(invoked, [True])
        self.assertEqual([message["status"] for message in sent if message["type"] == "http.response.start"], [413])

    def test_non_upload_routes_are_not_limited(self):
        invoked, sent = self.run_middleware(
            self.upload_scope(path="/api/auth/login", headers=[(b"content-length", b"999")]),
            [{"type": "http.request", "body": b"12345678901", "more_body": False}],
        )
        self.assertEqual(invoked, [True])
        self.assertEqual([message["status"] for message in sent if message["type"] == "http.response.start"], [204])


if __name__ == "__main__":
    unittest.main()
