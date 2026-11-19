"""Rasterize the visible content area of a public notice for local OCR."""
from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urljoin, urlsplit


MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_IMAGES = 4
MAX_SEGMENTS = 4
MAX_SECONDS = 20.0
_render_slots = threading.BoundedSemaphore(1)
LABEL = "官网截图 OCR"
_ROSTER_WORDS = re.compile(r"名单|人选|花名册|排名|roster|winners", re.I)


def _image_bytes(source, page, deadline):
    from .safe_http import PublicTransport
    from .search import UA, robots_allows

    import httpx

    data_url = re.fullmatch(r"data:image/(?:png|jpeg);base64,([A-Za-z0-9+/=]+)", source)
    if data_url:
        encoded = data_url[1]
        if len(encoded) > (MAX_IMAGE_BYTES * 4 // 3 + 4):
            return b""
        try:
            return base64.b64decode(encoded, validate=True)
        except binascii.Error:
            return b""
    try:
        url = urljoin(page, source)
        target, origin = urlsplit(url), urlsplit(page)
    except ValueError:
        return b""
    if (target.scheme not in {"http", "https"} or target.netloc != origin.netloc
            or origin.scheme == "https" and target.scheme != "https"
            or target.username or target.password or time.monotonic() >= deadline
            or not robots_allows(url)):
        return b""
    left = min(6.0, deadline - time.monotonic())
    if left <= 0:
        return b""
    try:
        with httpx.Client(transport=PublicTransport(), timeout=left, trust_env=False) as client:
            with client.stream("GET", url, headers={"User-Agent": UA, "Referer": page}) as response:
                if response.status_code != 200:
                    return b""
                result = bytearray()
                for chunk in response.iter_bytes(chunk_size=64 * 1024):
                    result.extend(chunk)
                    if len(result) > MAX_IMAGE_BYTES or time.monotonic() >= deadline:
                        return b""
        return bytes(result)
    except (httpx.HTTPError, ValueError):
        return b""


def _screenshots(heading, image):
    import pymupdf

    # JPEG/PNG only. The supplied page never controls markup, filesystem paths,
    # external fonts, or executable content in the snapshot renderer.
    filetype = ("png" if image.startswith(b"\x89PNG\r\n\x1a\n")
                else "jpeg" if image.startswith(b"\xff\xd8\xff") else None)
    if filetype is None:
        return
    with pymupdf.open(stream=image, filetype=filetype) as artwork:
        source = artwork[0]
        if not (140 <= source.rect.width <= 12000 and 70 <= source.rect.height <= 16000) or source.rect.get_area() > 38_000_000:
            return
        scale = min(1.5, 1440 / source.rect.width)
        strip_height = 1300 / scale
        for index in range(MAX_SEGMENTS):
            top = index * (strip_height - 64 / scale)
            if top >= source.rect.height:
                break
            area = pymupdf.Rect(0, top, source.rect.width, min(top + strip_height, source.rect.height))
            cut = source.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=area)
            width = max(580, cut.width + 32)
            with pymupdf.open() as snapshot:
                page = snapshot.new_page(width=width, height=cut.height + 88)
                if heading:
                    page.insert_textbox((16, 10, width - 16, 64), heading[:130], fontname="china-s", fontsize=18)
                page.insert_image((16, 70, 16 + cut.width, 70 + cut.height), pixmap=cut)
                yield page.get_pixmap().tobytes("png")
            if area.y1 >= source.rect.height:
                break


def recognize(url, html, *, timeout=MAX_SECONDS):
    """Read an image notice as a screenshot, returning its OCR text and a reason."""
    from bs4 import BeautifulSoup
    from .search import robots_allows

    if "<" not in html[:2000] or len(html) > 2 * 1024 * 1024:
        return "", ""
    soup = BeautifulSoup(html, "lxml")
    if soup.select_one("input[type=password]"):
        return "", ""
    for node in soup.select("script, style, template, noscript, nav, header, footer, aside, form, [hidden], [aria-hidden=true]"):
        if node.parent is not None:
            node.decompose()
    main = (soup.select_one(".v_news_content, .wp_articlecontent, .article-content, .article-body, .field-body, #divContent, [itemprop=articleBody]")
            or soup.find("article") or soup.find("main") or soup.body or soup)
    heading = soup.find("h1") or soup.title
    heading = heading.get_text(" ", strip=True) if heading else ""
    if re.search(r"验证码|访问受限|安全验证|请先登录|captcha|access denied", heading, re.I):
        return "", ""
    sources = {}
    for img in main.find_all("img"):
        if re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", img.get("style", ""), re.I):
            continue
        if any(str(img.get(attr, "")).strip() in {"0", "1", "0px", "1px"} for attr in ("width", "height")):
            continue
        source = (img.get("data-original") or img.get("data-src") or img.get("src") or "").strip()
        if not source:
            continue
        figure = img.find_parent("figure")
        caption = figure.find("figcaption") if figure else None
        block = figure or img.find_parent(["p", "div"]) or img
        context = block.find_previous_sibling(["p", "h2", "h3", "h4"])
        metadata = " ".join([img.get("alt", ""), img.get("title", ""),
                             caption.get_text(" ", strip=True) if caption else ""])
        score = 2 if _ROSTER_WORDS.search(metadata) else 0
        if context and _ROSTER_WORDS.search(context.get_text(" ", strip=True)[-130:]):
            score += 1
        sources[source] = max(sources.get(source, 0), score)
    if not sources:
        return "", ""
    tesseract = shutil.which("tesseract")
    if not tesseract:
        return "", "服务器未安装本地 OCR"
    if not robots_allows(url):
        return "", "官网 robots.txt 不允许访问该页"
    deadline = time.monotonic() + min(MAX_SECONDS, timeout)
    if not _render_slots.acquire(timeout=max(0, deadline - time.monotonic())):
        return "", "等待页面截图时达到时限"
    try:
        ocr_env = {name: os.environ[name] for name in ("LANG", "LC_ALL", "TESSDATA_PREFIX") if name in os.environ}
        ocr_env["OMP_THREAD_LIMIT"] = "1"
        recognized = []
        segments = 0
        had_timeout = False
        for source in sorted(sources, key=sources.get, reverse=True)[:MAX_IMAGES]:
            if time.monotonic() >= deadline or segments >= MAX_SEGMENTS:
                break
            data = _image_bytes(source, url, deadline)
            if not data:
                continue
            try:
                for image in _screenshots(heading, data):
                    left = deadline - time.monotonic()
                    if left <= 0 or segments >= MAX_SEGMENTS:
                        break
                    segments += 1
                    result = subprocess.run(
                        [tesseract, "stdin", "stdout", "-l", "chi_sim+eng", "--psm", "6"],
                        input=image, capture_output=True, timeout=min(left, 8.0), check=False,
                        env=ocr_env,
                    )
                    if result.returncode == 0:
                        raw = result.stdout.decode("utf-8", errors="replace").strip()
                        if raw:
                            recognized.append(raw)
            except subprocess.TimeoutExpired:
                had_timeout = True
            except (OSError, RuntimeError, ValueError):
                continue
        if recognized:
            return "\n［中间原文省略］\n".join(recognized), ""
        if had_timeout:
            return "", "官网截图 OCR 超时"
        return "", "内嵌图片未能读取或截图未识别到可读文字"
    finally:
        _render_slots.release()
