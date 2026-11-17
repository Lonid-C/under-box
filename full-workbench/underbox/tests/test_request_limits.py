import io
import sys
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serve


def handler(length, body=b"", content_type="application/json"):
    h = object.__new__(serve.Handler)
    h.headers = Message()
    h.headers["Content-Length"] = str(length)
    h.headers["Content-Type"] = content_type
    h.rfile = io.BytesIO(body)
    h.connection = SimpleNamespace(settimeout=lambda seconds: None)
    h.responses = []
    h._json = lambda data, status=200, **kwargs: h.responses.append((status, data))
    return h


class RequestLimitTests(unittest.TestCase):
    def test_huge_json_is_rejected_before_read(self):
        h = handler(65537)
        with self.assertRaises(serve.RequestBodyRejected):
            h._read_json()
        self.assertEqual(h.responses[0][0], 413)
        self.assertEqual(h.rfile.tell(), 0)

    def test_non_object_and_bad_json_return_400(self):
        for raw in (b"[]", b"null", b"{"):
            h = handler(len(raw), raw)
            with self.assertRaises(serve.RequestBodyRejected):
                h._read_json()
            self.assertEqual(h.responses[0][0], 400)

    def test_negative_and_duplicate_length_rejected(self):
        h = handler(-1)
        self.assertIsNone(h._read_body(100))
        self.assertEqual(h.responses[0][0], 400)
        h = handler(2, b"{}")
        h.headers["Content-Length"] = "2"
        self.assertIsNone(h._read_body(100))
