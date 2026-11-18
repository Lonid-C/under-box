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


class OCRText(str):
    """A text view of an image document, used only as a lead."""


def recognize_scanned_pdf(data: bytes, *, timeout=55):
    try:
        import pymupdf
        with pymupdf.open(stream=data, filetype="pdf") as document:
            if document.needs_pass:
                return None, "PDF 受密码保护，无法提取正文"
            count = len(document)
            if count > 20:
                return None, "扫描 PDF 超过单份自动 OCR 页数上限（20 页）"
            if not count or any(page.get_text().strip() for page in document):
                return None, "PDF 文本层未能可靠读取"
            if not any(page.get_images() for page in document):
                return None, "PDF 无可读文本，也未识别出扫描图像"
    except Exception:
        return None, "PDF 文件结构无法解析"
    if not os.environ.get("DEEPSEEK_API_KEY"):
        return None, "扫描 PDF 无文本层，OCR 服务未配置"
    if timeout <= 0:
        return None, "扫描 PDF 无文本层，已达到读取时限"
    from .deepseek_client import DeepSeekClient, DeepSeekError
    from .deepseek_document import _ocr_text
    try:
        pages = DeepSeekClient().ocr_bytes(data, "public-roster.pdf", timeout=timeout)
    except DeepSeekError as error:
        status = f"（HTTP {error.status}）" if error.status else ""
        return None, "扫描 PDF OCR 服务未能完成识别" + status
    except Exception:
        return None, "扫描 PDF OCR 返回结果无法解析"
    if sorted(index for index, _ in pages) != list(range(1, count + 1)):
        return None, "扫描 PDF OCR 返回页数不完整"
    text = _ocr_text(pages).strip() if any(raw.strip() for _, raw in pages) else ""
    if not text:
        return None, "扫描 PDF OCR 未识别到可读文字"
    return OCRText(text), ""


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
    # Public roster images can be sent to DeepSeek OCR. Returned text is
    # marked as OCR by the collector and never independently establishes "ok".
    if os.environ.get("DEEPSEEK_API_KEY"):
        try:
            from .deepseek_client import DeepSeekClient
            from .deepseek_document import _ocr_text
            suffix = ".png" if data.startswith(b"\x89PNG") else ".jpg" if data.startswith(b"\xff\xd8") else ".webp"
            pages = DeepSeekClient().ocr_bytes(data, "roster" + suffix, timeout=max(timeout, 30))
            if any(raw.strip() for _, raw in pages):
                text = _ocr_text(pages)
                if len(text.strip()) > 4:
                    return text
        except Exception:
            pass
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
