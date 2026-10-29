"""有界读取公开旧 Office 名单；只解析文档，不运行宏或脚本。"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

MAX_BYTES = 12 * 1024 * 1024
MAX_ROWS = 20000
MAX_CHARS = 400000


def legacy_office_text(data: bytes) -> str | None:
    if not data.startswith(b"\xd0\xcf\x11\xe0") or len(data) > MAX_BYTES:
        return None
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
                    if size > MAX_CHARS: return "\n".join(lines) or None
                    if line: lines.append(line)
                if used >= MAX_ROWS: break
            return "\n".join(lines).strip() or None
        finally:
            book.release_resources()
    except Exception:
        pass
    reader = shutil.which("antiword")
    if not reader:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="underbox-public-doc-") as folder:
            path = Path(folder)/"roster.doc"; path.write_bytes(data)
            with tempfile.TemporaryFile() as output:
                result = subprocess.run(["/bin/sh", "-c", 'ulimit -f 2048; exec "$@"', "public-doc", reader,
                         "-m", "UTF-8.txt", str(path)], stdout=output, stderr=subprocess.DEVNULL, timeout=15)
                if result.returncode: return None
                output.seek(0); text = output.read(MAX_CHARS * 4 + 1)
                if len(text) > MAX_CHARS * 4: return None
                return text.decode("utf-8", errors="replace").strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None
