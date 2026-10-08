"""读取官网ZIP/RAR名单包；只向stdout取名单文档，不把成员路径解压到磁盘。"""
from __future__ import annotations

import re
import io
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath


def archive_text(data: bytes, *, hint: str = "", pdf_parser, office_parser,
                 _depth: int = 0, _deadline: float | None = None) -> str | None:
    tar = shutil.which("bsdtar") or ("/usr/bin/tar" if Path("/usr/bin/tar").exists() else None)
    is_zip = zipfile.is_zipfile(io.BytesIO(data))
    if (not tar and not is_zip) or len(data) > 64 * 1024 * 1024:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="underbox-roster-archive-") as folder:
            archive = Path(folder) / "rosters"
            archive.write_bytes(data)
            zipped = zipfile.ZipFile(io.BytesIO(data)) if is_zip else None
            if zipped:
                names = zipped.namelist()
            else:
                listing = subprocess.run([tar, "-tf", str(archive)],capture_output=True,timeout=10)
                if listing.returncode:
                    return None
                names = listing.stdout.decode("utf-8", errors="replace").splitlines()
            files = []
            for name in names[:500]:
                path = PurePosixPath(name)
                if (path.is_absolute() or ".." in path.parts or name.startswith("-")
                        or not re.search(r"\.(?:pdf|xlsx|docx|zip|rar)$",name,re.I)
                        or re.search(r"教师|组织|优胜学校",name)):
                    continue
                if re.search(r"算法|C/C\+\+|C\+\+|Java|Python|程序|软件",hint,re.I) and re.search(r"设计赛|电子类",name):
                    continue
                files.append(name)
            from .competitions import _PROVINCES
            preferred = [province for province in _PROVINCES if province in hint]
            if preferred:
                files = [name for name in files if not any(province in name for province in _PROVINCES)
                         or any(province in name for province in preferred)]
            files.sort(key=lambda name:(not any(province in name for province in preferred) if preferred else False,
                                        "软件类" not in name, "研究生" in name, name))
            out = []
            total = 0
            deadline = _deadline or time.monotonic()+25
            for name in files[:16]:
                if time.monotonic()>=deadline:
                    break
                nested = name.lower().endswith((".rar", ".zip"))
                if nested and _depth >= 2:
                    continue
                cap = 48*1024*1024 if nested else 12*1024*1024
                if zipped:
                    if zipped.getinfo(name).file_size > cap:
                        continue
                    with zipped.open(name) as stream:
                        blob=stream.read(cap+1)
                else:
                    with tempfile.TemporaryFile() as stream:
                        # 子进程文件上限防止超大成员；文件名只作为独立参数，不拼进shell代码。
                        run = subprocess.run(["/bin/sh","-c",'ulimit -f 65536; exec "$@"',"roster-reader",
                                              tar,"-xOf",str(archive),name],stdout=stream,stderr=subprocess.DEVNULL,timeout=min(8,max(0.1,deadline-time.monotonic())))
                        if run.returncode:
                            continue
                        stream.seek(0);blob=stream.read(cap+1)
                if len(blob)>cap:
                    continue
                total += len(blob)
                if total>64*1024*1024:
                    break
                if nested:
                    text = archive_text(blob,hint=hint,pdf_parser=pdf_parser,office_parser=office_parser,
                                        _depth=_depth+1,_deadline=deadline)
                else:
                    text = pdf_parser(blob) if blob.startswith(b"%PDF-") else office_parser(blob)
                if text and text.strip():
                    out.append(f"\n［名单包内文件：{name}］\n{text}")
            if zipped:
                zipped.close()
            return "\n".join(out) or None
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, subprocess.SubprocessError):
        return None
