import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "underbox"))
sys.path.insert(0, str(ROOT / "open_box"))
import serve
import store
from app.usage import UsageMeter

class FakeStream:
    status_code = 200
    def iter_lines(self):
        for chunk in [
            {"choices":[{"server_tool_event":{"type":"call_completed","call":{"status":"completed","sources":[
                {"url":"https://school.example/a","title":"学校"},
                {"url":"http://127.0.0.1/private","title":"private"}]}},"delta":{}}]},
            {"choices":[{"delta":{"content":"报告回答"}}]},
            {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"server_tool_use":{"tool_search":1}}},
        ]:
            yield "data: " + json.dumps(chunk)
        yield "data: [DONE]"

class IntegrationTests(unittest.TestCase):
    def test_native_search_sources_and_stream(self):
        @contextmanager
        def fake(_self, body):
            self.assertEqual(body["tools"][0]["type"], "deepseek_web_search")
            yield FakeStream()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test"}), \
             patch("app.deepseek_client.DeepSeekClient.stream_chat", fake):
            events = list(serve.stream_answer("报告", "问题", {"_native":True}))
        source = next(e["search"]["sources"] for e in events if "search" in e)
        self.assertEqual([s["url"] for s in source], ["https://school.example/a"])
        self.assertEqual(next(e["delta"] for e in events if "delta" in e), "报告回答")
        self.assertFalse(any("error" in e for e in events))

    def test_usage_migration_and_unknown_tool_price(self):
        meter = UsageMeter(); base=meter.mark(); meter.record_tool("search",2)
        usage=meter.since(base)
        self.assertIsNone(usage["deepseek"]["yuan"])
        with tempfile.TemporaryDirectory() as d:
            path=store.init(Path(d)/"test.db")
            with store.connect(path) as conn:
                store.record_usage(conn,"test",kind="verify",usage=usage)
                row=conn.execute("select ds_tools,yuan from usage_log").fetchone()
                self.assertEqual(json.loads(row["ds_tools"])["search"],2)
                self.assertIsNone(row["yuan"])
                summary=store.user_usage(conn,"test")
                self.assertEqual(summary["deepseekTools"]["search"],2)
                self.assertIsNone(summary["yuan"])

if __name__ == "__main__": unittest.main()
