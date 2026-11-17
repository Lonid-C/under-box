import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import serve
from app.llm import OpenAICompatLLM


class ChatProviderTests(unittest.TestCase):
    def test_generic_provider_does_not_receive_vendor_parameters(self):
        sent = []
        class Response:
            status_code = 200
            def iter_lines(self):
                return iter(['data: {"choices":[{"delta":{"content":"合成回答"}}]}', 'data: [DONE]'])
        @contextmanager
        def fake(*args, **kwargs):
            sent.append(kwargs["json"])
            yield Response()
        llm = OpenAICompatLLM(endpoint="https://model.example/chat", api_key="synthetic-key", model="synthetic")
        self.assertEqual(list(llm.stream_text("规则", "合成数据", stream_factory=fake)), [{"delta": "合成回答"}])
        self.assertNotIn("thinking", sent[0])
        self.assertNotIn("stream_options", sent[0])

    def test_http_error_body_cannot_leak_secrets(self):
        class Response:
            status_code = 401
            text = "Authorization: synthetic-secret"
        @contextmanager
        def fake(*args, **kwargs):
            yield Response()
        llm = OpenAICompatLLM(endpoint="https://model.example/chat", api_key="synthetic-secret", model="synthetic")
        events = list(llm.stream_text("规则", "数据", stream_factory=fake))
        self.assertNotIn("synthetic-secret", str(events))
        self.assertIn("401", str(events))
