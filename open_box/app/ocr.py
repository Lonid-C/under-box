"""有时间/体积上限的本机名单图片 OCR；不可用时明确返回失败，不编造文字。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading

_build_lock = threading.Lock()


def _vision_binary() -> Path | None:
    if sys.platform != "darwin" or not shutil.which("swiftc"):
        return None
    source = Path(__file__).with_name("ocr_vision.swift")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    folder = Path(tempfile.gettempdir()) / f"underbox-ocr-{os.getuid()}"
    folder.mkdir(mode=0o700, exist_ok=True)
    binary = folder / f"vision-{digest}"
    with _build_lock:
        if not binary.exists():
            result = subprocess.run([shutil.which("swiftc"), "-O", str(source), "-o", str(binary)],
                                    capture_output=True, timeout=30)
            if result.returncode:
                return None
    return binary


def _lines(observations: list[dict]) -> str:
    groups = []
    seen = set()
    for row in sorted(observations, key=lambda r: (r.get("top", 0), r.get("left", 0))):
        text = str(row.get("text") or "").strip()
        if not text or float(row.get("confidence", 0)) < 0.8:
            continue
        key = (text, round(float(row.get("top", 0)), 3), round(float(row.get("left", 0)), 2))
        if key in seen:
            continue
        seen.add(key)
        top = float(row.get("top", 0))
        if groups and abs(top - groups[-1][0]) <= max(0.002, float(row.get("height", 0)) * 0.5):
            groups[-1][1].append(row)
        else:
            groups.append((top, [row]))
    return "\n".join(" | ".join(str(r["text"]) for r in sorted(rows, key=lambda r: r.get("left", 0)))
                     for _, rows in groups)


def recognize_image(data: bytes, *, timeout: float = 15) -> str | None:
    if not data or len(data) > 8 * 1024 * 1024:
        return None
    try:
        binary = _vision_binary()
        if not binary:
            return None
        with tempfile.TemporaryDirectory(prefix="underbox-public-ocr-") as folder:
            path = Path(folder) / "roster-image"
            path.write_bytes(data)
            result = subprocess.run([str(binary), str(path)], capture_output=True, timeout=timeout)
        if result.returncode:
            return None
        observations = json.loads(result.stdout)
        return _lines(observations) or None if isinstance(observations, list) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
