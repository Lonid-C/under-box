"""测试一律不做现场栏目发现、不写仓库里的栏目缓存（见 app/discover.py）。"""
import os
import tempfile

os.environ.setdefault("ARCHIVE_DISCOVERY", "off")
os.environ.setdefault("ARCHIVE_CACHE", os.path.join(tempfile.mkdtemp(prefix="archive-cache-"), "cache.json"))
