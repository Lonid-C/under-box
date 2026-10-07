"""测试一律不做现场栏目发现、不写仓库里的栏目缓存（见 app/discover.py）。"""
import os
import tempfile

os.environ.setdefault("ARCHIVE_DISCOVERY", "off")
os.environ.setdefault("ARCHIVE_CACHE", os.path.join(tempfile.mkdtemp(prefix="archive-cache-"), "cache.json"))
# 测试里的模型桩按调用顺序回答（画像 → 拆分 → 各条澄清问题），陈述并行会打乱这个顺序，
# 所以默认逐条串行；并行路径由 tests/test_parallel.py 单独覆盖（它自己改这个变量）。
os.environ.setdefault("CLAIM_WORKERS", "1")
