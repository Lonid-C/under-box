import io
import json
import os
import sys
import unittest
import wave
from pathlib import Path
from unittest.mock import patch
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serve import Handler, voice_allowed, _VOICE_CALLS

def handler(body=b''):
    h = object.__new__(Handler)
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body)
    h._st = lambda: {"oby": object()}
    h._json = lambda data, status=200: (status, data)
    h._send = lambda data, ctype, status=200: (status, ctype, data)
    return h

def wav():
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
        f.writeframes(b"\0\0" * 1000)
    return out.getvalue()

class VoiceTests(unittest.TestCase):
    def test_asr_returns_editable_text(self):
        h = handler(wav())
        req = httpx.Request("POST", "https://api.deepseek.com/v1/tools/asr")
        response = httpx.Response(200, request=req, json={"text": "你好", "duration_ms": 62})
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test"}), patch("httpx.post", return_value=response):
            status, result = h._voice_asr()
        self.assertEqual((status, result["text"]), (200, "你好"))

    def test_streaming_wav_with_unknown_length_is_accepted(self):
        raw = bytearray(wav())
        raw[40:44] = (0xFFFFFFFF).to_bytes(4, "little")
        h = handler(bytes(raw))
        req = httpx.Request("POST", "https://api.deepseek.com/v1/tools/asr")
        response = httpx.Response(200, request=req, json={"text": "你好", "duration_ms": 62})
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test"}), patch("httpx.post", return_value=response):
            self.assertEqual(h._voice_asr()[0], 200)

    def test_invalid_audio_is_not_forwarded(self):
        h = handler(b"x" * 100)
        with patch("httpx.post") as post:
            self.assertEqual(h._voice_asr()[0], 400)
            post.assert_not_called()

    def test_tts_forwards_valid_wav(self):
        h = handler(json.dumps({"text":"你好"}).encode())
        req = httpx.Request("POST", "https://api.deepseek.com/v1/tools/tts")
        response = httpx.Response(200, request=req, content=wav())
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY":"test"}), patch("httpx.post", return_value=response):
            status, mime, result = h._voice_tts()
        self.assertEqual((status, mime), (200, "audio/wav"))
        self.assertTrue(result.startswith(b"RIFF"))

    def test_rate_limit_is_scoped_per_user(self):
        _VOICE_CALLS.clear()
        self.assertTrue(all(voice_allowed("one", "asr") for _ in range(10)))
        self.assertFalse(voice_allowed("one", "asr"))
        self.assertTrue(voice_allowed("two", "asr"))
        self.assertTrue(voice_allowed("one", "tts"))
        _VOICE_CALLS.clear()

    def test_no_report_no_voice(self):
        h=handler(wav());h._st=lambda: {"oby": None}
        self.assertEqual(h._voice_asr()[0], 409)

if __name__ == "__main__": unittest.main()
