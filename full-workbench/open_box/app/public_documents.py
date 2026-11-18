"""有界读取公开旧 Office 名单；只解析文档，不运行宏或脚本。"""
from __future__ import annotations

import shutil
import os
import subprocess
import tempfile
from pathlib import Path

MAX_BYTES = 12 * 1024 * 1024
MAX_ROWS = 20000
MAX_CHARS = 400000


def legacy_office_text(data: bytes) -> str | None:
    return legacy_office_read(data)[0]


def legacy_office_read(data: bytes) -> tuple[str | None, str]:
    if not data.startswith(b"\xd0\xcf\x11\xe0") or len(data) > MAX_BYTES:
        return None, "旧版 Office 附件格式不支持或超过读取大小"
    try:
        import xlrd
        book = xlrd.open_workbook(file_contents=data, on_demand=True, ragged_rows=True)
        lines = []; size = 0; used = 0
        try:
            for sheet in book.sheets()[:12]:
                lines.append("［工作表：" + sheet.name + "］")
                for row in range(min(sheet.nrows, MAX_ROWS-used)):
                    cells = []
                    for col in range(min(sheet.row_len(row), 80)):
                        cell = sheet.cell(row, col); value = cell.value
                        if cell.ctype == xlrd.XL_CELL_NUMBER and float(value).is_integer():
                            value = int(value)
                        elif cell.ctype == xlrd.XL_CELL_DATE:
                            value = xlrd.xldate_as_datetime(value, book.datemode).isoformat(sep=" ")
                        cells.append(str(value).replace("\r", " ").replace("\n", " ").strip())
                    line = " | ".join(cells).strip(" |")
                    used += 1; size += len(line)
                    if size > MAX_CHARS: return ("\n".join(lines) or None), ""
                    if line: lines.append(line)
                if used >= MAX_ROWS: break
            text = "\n".join(lines).strip()
            return text or None, "" if text else "旧版 XLS 附件没有文本"
        finally:
            book.release_resources()
    except Exception:
        pass
    from .retrieval_policy import enabled as ict_recall
    reader = (os.environ.get('UNDERBOX_ICT_DOC_READER') if ict_recall() else None) or shutil.which("antiword")
    if not reader:
        return None, "旧版 DOC 读取器不可用"
    env = os.environ.copy()
    if ict_recall() and (home := env.get("UNDERBOX_ICT_DOC_HOME")):
        mapping = Path(home) / ".antiword"
        if not os.access(mapping / "UTF-8.txt", os.R_OK):
            return None, "旧版 DOC 读取器无法读取 UTF-8 字符映射"
        # The packaged binary searches HOME/.antiword on this distribution.
        env.update(HOME=home, ANTIWORDHOME=str(mapping))
    try:
        with tempfile.TemporaryDirectory(prefix="underbox-public-doc-") as folder:
            path = Path(folder)/"roster.doc"; path.write_bytes(data)
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(["/bin/sh", "-c", 'ulimit -f 2048; exec "$@"', "public-doc", reader,
                         "-m", "UTF-8.txt", str(path)], stdout=output, stderr=subprocess.PIPE, timeout=15, env=env)
                if result.returncode:
                    error = (getattr(result, "stderr", b"") or b"").lower()
                    if b"mapping file" in error:
                        return None, "旧版 DOC 读取器无法读取 UTF-8 字符映射"
                    if b"password" in error or b"encrypt" in error:
                        return None, "旧版 DOC 附件受密码保护"
                    return None, "旧版 DOC 读取器无法解析附件内容"
                output.seek(0); text = output.read(MAX_CHARS * 4 + 1)
                if len(text) > MAX_CHARS * 4: return None, "旧版 DOC 附件超过可读取文本大小"
                text = text.decode("utf-8", errors="replace").strip()
                return text or None, "" if text else "旧版 DOC 附件未返回可读文字"
    except subprocess.TimeoutExpired:
        return None, "旧版 DOC 附件解析超时"
    except OSError:
        return None, "旧版 DOC 读取器启动失败"
