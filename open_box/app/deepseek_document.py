"""File parsing for the web upload flow.

DeepSeek's Files API does not return parsed text. We retain locally extracted
text as the source of truth, using OCR when the local text layer is absent.
The Files transcript is accepted only when it agrees with the source text.
"""
from __future__ import annotations
import re
from pathlib import Path
from .deepseek_client import DeepSeekClient, DeepSeekIncomplete
from .parse import (ParsedDocument, detect_document_type, parse_document,
                    detect_injection, detect_pdf_visual_risks)


def _compact(text):
    return re.sub(r"\s+", "", text or "")


def _ocr_text(pages):
    cleaned = []
    for number, raw in pages:
        # OCR's layout tokens and normalized bounding boxes are metadata, not text.
        text = re.sub(r"<[｜|]box[｜|]>\s*\[\[.*?\]\]\s*<\s*[｜|]/box[｜|]>", "", raw, flags=re.S)
        text = re.sub(r"<[｜|][^<>]*?[｜|]>", "", text)
        cleaned.append(f"第{number}页\n{text.strip()}")
    return "\n".join(cleaned)


def _pdf_native_pages(path: Path):
    """Return page text to identify mixed text and scanned pages."""
    try:
        import fitz
        with fitz.open(str(path)) as document:
            return [(page.get_text(), bool(page.get_images() or page.get_drawings())) for page in document]
    except (ImportError, OSError, ValueError):
        return None


def parse_deepseek_document(path: str | Path, *, client=None):
    p = Path(path)
    kind = detect_document_type(p, allow_legacy=True)
    native = None
    if kind != "doc":
        try:
            native = parse_document(p)
        except ValueError as exc:
            if "没有可提取的简历正文" not in str(exc):
                raise
    client = client or DeepSeekClient()
    with client.upload_file(p, kind) as file_id:
        try:
            transcript = client.file_text(file_id)
        except DeepSeekIncomplete:
            # A truncated or empty model transcript is never used as source text.
            transcript = ""
        native_pages = _pdf_native_pages(p) if kind == "pdf" else None
        need_ocr = (native is None or len(_compact(native.raw_text)) < 40
                    or (kind == "pdf" and (native_pages is None
                        or any(len(_compact(text)) < 40 and visual for text, visual in native_pages))))
        if not need_ocr:
            # Never let a generative transcription introduce or omit original text.
            raw = transcript if _compact(transcript) == _compact(native.raw_text) else native.raw_text
            risks = native.risks
        else:
            if kind == "text":
                raise ValueError("文件没有可提取的简历正文")
            ocr_pages = client.ocr(p)
            if native_pages is not None and len(native_pages) == len(ocr_pages):
                merged = []
                for index, (number, text) in enumerate(ocr_pages):
                    local, visual = native_pages[index]
                    # Preserve the richer native text layer and truly blank pages.
                    if len(_compact(local)) >= 40 or not visual:
                        merged.append(f"第{number}页\n{local}")
                    else:
                        cleaned = _ocr_text([(number, text)])
                        merged.append(cleaned if len(_compact(text)) >= len(_compact(local))
                                      else f"第{number}页\n{local}")
                raw = "\n".join(merged)
            else:
                raw = _ocr_text(ocr_pages)
            if len(_compact(raw)) < 40:
                raise ValueError("文件没有可提取的简历正文")
            risks = list(native.risks) if native else (detect_pdf_visual_risks(p) if kind == "pdf" else [])
            risks += detect_injection(raw)
        result = ParsedDocument(raw, risks, str(p), kind)
        if not result.safe_text.strip():
            raise ValueError("文件没有可提取的简历正文")
        return result
