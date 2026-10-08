"""搜索封装。接口固定为 search(query, site=None) -> list[SearchHit]。

供应商在这一层切换，业务代码不认识任何一家 API。
所有外部请求：User-Agent 标明用途与联系方式，遵守 robots.txt，
单域名不超过 1 次/秒，失败重试不超过 2 次（第 7 节）。
"""
from __future__ import annotations

import os
import posixpath
import re
import threading
import time
import zipfile
from xml.etree import ElementTree as ET
import urllib.robotparser as robotparser
from io import BytesIO
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol
from urllib.parse import urlparse

def _ascii_header(value: str, fallback: str) -> str:
    """HTTP 头必须是 latin-1 可编码的。

    用中文写 User-Agent 会让 httpx 在发请求时抛 UnicodeEncodeError，
    而且是在重试循环里被吞掉，表现为"搜索永远返回空"——很难查。
    这里统一兜住：不可编码就退回纯 ASCII 的默认值。
    """
    try:
        value.encode("latin-1")
        return value
    except (UnicodeEncodeError, AttributeError):
        return fallback


_DEFAULT_UA = ("ResumeVerifyDemo/0.1 (+resume verification demo; "
               "reads public pages only; contact: demo@example.invalid)")
UA = _ascii_header(os.environ.get("HTTP_USER_AGENT", _DEFAULT_UA), _DEFAULT_UA)
RATE_LIMIT_SECONDS = 1.0
MAX_RETRIES = 2


def search_interval() -> float:
    """搜索 API 两次请求发出之间的最小间隔（秒）。学校官网读页仍按 RATE_LIMIT_SECONDS。

    陈述并行（pipeline.CLAIM_WORKERS）以后，所有陈述共用这一个搜索出口：原来 1 秒一次，
    十几条陈述的几十次搜索光排队就要一两分钟。默认 0.5 秒（每秒最多发出 2 个，响应可以
    重叠等待）；遇到 429 限流会退避重试。账号限额更高可调小，被限流就调大。
    """
    try:
        return max(0.0, float(os.environ.get("SEARCH_MIN_INTERVAL", "0.5")))
    except ValueError:
        return 0.5
# 2025 CUMCM 正式名单约 19 MB，旧 12 MB 限制会把真实名单误报为扫描件。
MAX_PUBLIC_PDF_BYTES = 32 * 1024 * 1024
MAX_PUBLIC_OFFICE_BYTES = 12 * 1024 * 1024


def _office_text(data: bytes) -> str | None:
    """按 ZIP 内部结构识别官网 DOCX/XLSX 名单，保留表格的逐行关系。"""
    if len(data) > MAX_PUBLIC_OFFICE_BYTES:
        return None
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 48 * 1024 * 1024:
                return None
            names = set(archive.namelist())
            if "word/document.xml" in names:
                root = ET.fromstring(archive.read("word/document.xml"))
                ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
                def word_text(node):
                    return "".join(t.text or "" for t in node.findall(".//w:t", ns))
                lines = []
                for node in root.findall("w:body/*", ns):
                    if node.tag.endswith("}tbl"):
                        for row in node.findall("w:tr", ns):
                            lines.append(" | ".join(word_text(c) for c in row.findall("w:tc", ns)))
                    else:
                        lines.append(word_text(node))
                return "\n".join(lines).strip() or None
            if "xl/workbook.xml" not in names:
                return None
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            strings = []
            if "xl/sharedStrings.xml" in names:
                root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
                strings = ["".join(node.itertext()) for node in root.findall("s:si", ns)]
            # 工作表文件由 workbook 的关系标识定位，保留页面标题中使用的 sheet 名称。
            relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
            targets = {r.attrib["Id"]: r.attrib.get("Target", "") for r in relations
                       if r.attrib.get("TargetMode") != "External"}
            workbook = ET.fromstring(archive.read("xl/workbook.xml"))
            lines = []
            for sheet in workbook.findall("s:sheets/s:sheet", ns):
                rid = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
                target = targets.get(rid, "")
                path = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
                if path not in names:
                    continue
                lines.append("工作表：" + sheet.get("name", ""))
                root = ET.fromstring(archive.read(path))
                for row in root.findall("s:sheetData/s:row", ns):
                    cells = []
                    for cell in row.findall("s:c", ns):
                        # 缺失的单元格要补空，姓名与“候补/录取”所在列不能发生错位。
                        column = re.match(r"([A-Z]+)", cell.get("r", ""))
                        index = 0
                        if column:
                            for letter in column.group():
                                index = index * 26 + ord(letter) - ord("A") + 1
                            if index > 16384:
                                return None
                            cells.extend([""] * max(0, index - 1 - len(cells)))
                        value = cell.findtext("s:v", default="", namespaces=ns)
                        if cell.get("t") == "s" and value.isdigit():
                            value = strings[int(value)] if int(value) < len(strings) else ""
                        elif cell.get("t") == "inlineStr":
                            value = "".join(t.text or "" for t in cell.findall("s:is//s:t", ns))
                        cells.append(value)
                    if any(cells):
                        lines.append(" | ".join(cells))
            return "\n".join(lines) or None
    except (KeyError, ValueError, AttributeError, zipfile.BadZipFile, ET.ParseError):
        return None


def _award_table_text(document) -> str:
    """小型中文获奖表按行读取，仅延续已识别奖项列的合并单元格。"""
    import unicodedata
    if len(document) > 60:
        return ""
    first = unicodedata.normalize("NFKC", document[0].get_text())
    if "姓名" not in first or not re.search(r"奖项|奖级|获奖等级|奖励等级", first):
        return ""
    headers = None
    award_column = None
    previous_award = ""
    lines = []
    for page_no, page in enumerate(document, 1):
        try:
            tables = page.find_tables().tables
        except Exception:
            previous_award = ""
            continue
        if not tables:
            previous_award = ""
        for table in tables:
            for raw in table.extract():
                row = [" ".join(unicodedata.normalize("NFKC", str(c or "")).split()) for c in raw]
                if "姓名" in row and any(re.fullmatch(r"奖项|奖级|获奖等级|奖励等级", c) for c in row):
                    new_headers = row
                    if headers and headers != new_headers:
                        previous_award = ""
                    headers = new_headers
                    award_column = next(i for i,c in enumerate(row) if re.fullmatch(r"奖项|奖级|获奖等级|奖励等级",c))
                    continue
                if not headers or len(row) != len(headers) or not row[headers.index("姓名")]:
                    continue
                merged = not row[award_column]
                if row[award_column]:
                    previous_award = row[award_column]
                elif previous_award:
                    row[award_column] = previous_award
                if not row[award_column]:
                    continue
                lines.append(" | ".join(row) + f" ［第{page_no}页" + ("，奖项沿用合并单元格" if merged else "") + "］")
    return ("\n［获奖表格按行读取；空白奖项列依据同表合并单元格延续］\n"
            + " | ".join(headers) + "\n" + "\n".join(lines)) if lines else ""


def _pdf_text(data: bytes) -> str | None:
    """公开名单常是 PDF；按文件签名解析，不能把二进制当网页文字。"""
    if not data[:1024].lstrip().startswith(b"%PDF-") or len(data) > MAX_PUBLIC_PDF_BYTES:
        return None
    try:
        import pymupdf
        import unicodedata
        with pymupdf.open(stream=data, filetype="pdf") as document:
            text = unicodedata.normalize("NFKC", "\n".join(page.get_text() for page in document))
            try:
                rows = _award_table_text(document)
            except Exception:
                rows = ""
            if rows:
                text = text[:1000] + rows + "\n［原始文本］\n" + text
        if text.strip():
            return text
    except Exception:
        pass
    try:
        from pdfminer.high_level import extract_text
        # pdfminer 对纯扫描件会返回分页符，不能把“\f\f”认作已读出名单。
        text = extract_text(BytesIO(data))
        return text if text and text.strip() else None
    except Exception:
        return None


class SearchUnavailable(RuntimeError):
    """鉴权失败 / 余额不足 / 被拒。不重试，直接报给调用方。"""


class SearchFiltered(SearchUnavailable):
    """**这一条查询串**被内容审核拒了（HTTP 400 / code 1301 / contentFilter）。

    必须和 SearchUnavailable 分开：不是服务挂了、也不是余额不足，是审核对这一串字面
    动了手。实测（2026-09-22）它是**概率性误伤**——`"qingchuan-scheduler" "林昱和"`
    被拦，但换词序 `"林昱和" "qingchuan-scheduler"`、去引号、或追加一个上下文词都能过；
    同一串反复打也是时通时不通。同一时间 70 次连续请求全部 200，所以和限流无关。

    把它当 SearchUnavailable 一路抛到界面，一条被误伤的查询就会让整次核验跑到一半失败。
    调用方应当先用 rephrase_variants 改述重试，仍不过就**只跳过这一条**查询。
    """

    def __init__(self, message: str = "", *, query: str = "", site: str | None = None):
        super().__init__(message)
        self.query = query
        self.site = site


_CONTENT_FILTER_CODES = frozenset({"1301"})


def is_content_filtered(response) -> bool:
    """识别智谱的内容审核误拦。响应长这样（HTTP 400）：

        {"contentFilter": [{"level": 1, "role": "search"}],
         "error": {"code": "1301", "message": "系统检测到输入或生成内容可能包含…"}}

    只认 400 上的这两个标记。其它 400（参数写错、引擎名不对）不算，仍按普通失败重试——
    误判成审核会让真正的调用错误被静默跳过。
    """
    if getattr(response, "status_code", None) != 400:
        return False
    try:
        data = response.json()
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("contentFilter"):
        return True
    error = data.get("error")
    return isinstance(error, dict) and str(error.get("code") or "") in _CONTENT_FILTER_CODES


def rephrase_variants(query: str, extra: str = "") -> list[str]:
    """为被审核误拦的查询生成**同义**改述，按实测命中率排序。

    只做字面变换（词序轮转 / 去引号 / 补一个上下文词），绝不增删实体词——
    否则就不是"绕过误判"，而是"换了一条查询"，检索意图会变，召回结论不再可信。
    顺序来自实测：词序轮转最有效，其次轮转+去引号，再次单独去引号，最后补机构名。
    """
    tokens = [t for t in (query or "").split() if t]
    variants: list[str] = []

    def add(candidate: str) -> None:
        candidate = " ".join(candidate.split())
        if candidate and candidate != query and candidate not in variants:
            variants.append(candidate)

    unquoted = [t.strip('"').strip("'") for t in tokens]
    if len(tokens) >= 2:
        add(" ".join(tokens[1:] + tokens[:1]))
        add(" ".join(unquoted[1:] + unquoted[:1]))
    add(" ".join(unquoted))                      # 无引号时等于原串，会被 add 跳过
    if extra and extra not in tokens:
        add(" ".join(tokens + [extra]))
    return variants


@dataclass
class SearchHit:
    url: str
    title: str = ""
    snippet: str = ""
    publisher: str = ""
    # 结构化数据源（Crossref / GitHub API）已经返回了可引用的正文时放这里。
    # collect 层可直接抽取，避免再下载一个经常依赖 JS 或会拦截机器访问的展示页。
    content: str = ""
    # 由论文登记机构提供的期刊/出版社原文页面，仅作二次官网检索入口。
    official_url: str = ""
    # 论文检索需要结合作者、刊物和年份；单凭题名会把同名作品误并。
    authors: tuple[str, ...] = ()
    venue: str = ""
    year: str = ""


class Searcher(Protocol):
    def search(self, query: str, site: str | None = None) -> list[SearchHit]: ...


class NullSearcher:
    """没有配置搜索 key 时使用。返回空列表——查不到就是查不到，不编造。"""

    def search(self, query: str, site: str | None = None) -> list[SearchHit]:
        return []


class CachedSearcher:
    """只在单份报告内复用完全相同的查询；跨报告不共享个人信息或旧结果。"""

    def __init__(self, inner: Searcher):
        self.inner = inner
        self.cache: dict[tuple[str, str | None], list[SearchHit]] = {}
        self._guard = threading.Lock()
        self._locks: dict[tuple[str, str | None], threading.Lock] = {}
        self._confirmed: set[tuple[str, str | None]] = set()

    def _key_lock(self, key):
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def search(self, query: str, site: str | None = None) -> list[SearchHit]:
        key = (query, site)
        with self._key_lock(key):
            if key not in self.cache:
                self.cache[key] = self.inner.search(query, site=site)
            return list(self.cache[key])

    def search_with_confirmation(self, query: str, site: str | None = None,
                                 accept=None) -> list[SearchHit]:
        """只对计划标记的关键名单查询补查；同一空查询并行时也最多补查一次。"""
        key = (query, site)
        with self._key_lock(key):
            if key not in self.cache:
                self.cache[key] = self.inner.search(query, site=site)
            confirm = getattr(self.inner, "confirm_empty", None)
            useful = any(accept(hit) for hit in self.cache[key]) if accept else bool(self.cache[key])
            if not useful and site and key not in self._confirmed and callable(confirm):
                self._confirmed.add(key)
                found = confirm(query, site=site)
                urls = {hit.url for hit in self.cache[key]}
                self.cache[key] += [hit for hit in found if hit.url not in urls]
            return list(self.cache[key])


@dataclass
class FixtureSearcher:
    """按查询关键词返回预置结果，供测试与离线演示使用。"""

    table: dict[str, list[SearchHit]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def search(self, query: str, site: str | None = None) -> list[SearchHit]:
        q = f"site:{site} {query}" if site else query
        self.calls.append(q)
        for key, hits in self.table.items():
            if key in q:
                return list(hits)
        return []


def url_in_domain(url: str, domain: str) -> bool:
    """供应商过滤之外再校验主机边界，允许根域及学院子域。"""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    domain = domain.strip().lower().rstrip(".")
    return parsed.scheme in ("http", "https") and bool(domain) and (
        host == domain or host.endswith("." + domain))


class ZhipuSearcher:
    """智谱 Web Search API。

    选它的理由：中文索引对 *.edu.cn 的公示通知页覆盖好；`search_domain_filter` 是
    一等参数，正好对上本模块 search(query, site=) 的入参，不用把 site: 拼进查询串；
    引擎可切（search_std / search_pro / search_pro_sogou / search_pro_quark）。

    ⚠️ 端点默认值需要你用 `make search-check` 验一次——各家文档路径会变，
    验不过就用 SEARCH_ENDPOINT 覆盖，不要改代码。
    """

    DEFAULT_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/web_search"
    ENGINES = ("search_std", "search_pro", "search_pro_sogou", "search_pro_quark")

    def __init__(self, api_key: str = "", endpoint: str = "", engine: str = "",
                 timeout: float = 20.0):
        self.api_key = api_key or os.environ.get("SEARCH_API_KEY", "")
        self.endpoint = endpoint or os.environ.get("SEARCH_ENDPOINT") or self.DEFAULT_ENDPOINT
        # 单价（open.bigmodel.cn/pricing，2026-09 核）：
        #   search_std 0.01 / search_pro 0.03 / search_pro_sogou 0.05，**每次**。
        # 带域限定的查询占大头（学校官网公示、主办方站），这里默认用最便宜的 std——
        # 官方文档明确 std 支持 search_domain_filter，过滤本身是生效的，
        # 差别在召回深度。一份简历最多 12 条陈述 × 6 次搜索，std 与 pro 相差约 1.4 元。
        self.engine = engine or os.environ.get("SEARCH_ENGINE", "search_std")
        # 可选兜底：std 搜不到时，用更贵的引擎再确认一次，避免"便宜档没搜到"被
        # 当成"公开渠道没有"。默认**关闭**，因为它未必更省：
        #   期望单价 = 0.01 + p(空) × 0.03，而 p(空) > 2/3 时反而比直接用 pro 贵。
        #   学校官网这类窄域查询空结果本来就多，所以要不要开，看你的实际空率。
        # 打开：SEARCH_ENGINE_FALLBACK=search_pro
        self.engine_site_fallback = os.environ.get("SEARCH_ENGINE_FALLBACK", "")
        # **无域限定时要换引擎**（实测 2026-09-20）：
        #   search_std / search_pro 裸查询返回的 link 全是空的——内容、标题、日期都有，
        #   就是没 URL。`_parse` 会把没有 URL 的条目全丢掉，于是整次检索等于白搜，
        #   而且表面看"搜了 10 条"很正常。这个坑只有真打一次 API 才看得见。
        #   search_pro_sogou 裸查询给 50 条全带 link，但它的域限定对微信站失效；
        #   search_pro 的域限定是准的（pku.edu.cn → 只回 pku、微信 → 只回微信）。
        # 所以：带 site 用 self.engine（过滤生效），不带 site 用 self.engine_open。
        self.engine_open = os.environ.get("SEARCH_ENGINE_OPEN", "search_pro_sogou")
        self.timeout = timeout
        self.calls = 0
        self._last: dict[str, float] = {}
        self._throttle_lock = threading.Lock()

    def _throttle(self, host: str) -> None:
        # 并行查询只错峰发送；HTTP 等待可以重叠，同一搜索域相隔至少 search_interval() 秒。
        with self._throttle_lock:
            wait = search_interval() - (time.monotonic() - self._last.get(host, 0.0))
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    @staticmethod
    def _parse(payload: dict) -> list[SearchHit]:
        items = payload.get("search_result") or payload.get("results") or []
        out = []
        for it in items:
            url = it.get("link") or it.get("url") or ""
            if not url:
                continue
            out.append(SearchHit(
                url=url,
                title=it.get("title") or "",
                snippet=it.get("content") or it.get("snippet") or "",
                publisher=it.get("media") or it.get("site_name") or "",
            ))
        return out

    def search(self, query: str, site: str | None = None) -> list[SearchHit]:
        if not self.api_key:
            return []
        if not site:
            # 裸查询必须换引擎：search_std/search_pro 的裸结果没有 link（见 __init__）
            return self._call(query, None, self.engine_open)

        # 官方仅为 std/pro/sogou 声明域过滤支持；夸克配置不能使官网限定失效。
        engine = self.engine if self.engine != "search_pro_quark" else "search_pro"
        hits = self._call(query, site, engine)
        fallback = self.engine_site_fallback
        if not hits and fallback and fallback != engine:
            # 空结果不等于"不存在"，也可能是便宜档没挖到。升级重打一次，
            # 让"未找到公开记录"这句话是查过两遍才说的。
            hits = self._call(query, site, fallback)
        return hits

    def confirm_empty(self, query: str, site: str | None = None) -> list[SearchHit]:
        """关键整批名单的空结果用 pro 确认，不修改共享实例的默认引擎。"""
        if (not self.api_key or not site or self.engine != "search_std"
                or self.engine_site_fallback and self.engine_site_fallback != self.engine):
            return []
        return self._call(query, site, "search_pro")

    def _call(self, query: str, site: str | None, engine: str) -> list[SearchHit]:
        import httpx
        body: dict = {"search_query": query, "search_intent": False,
                      "search_engine": engine}
        if site:
            # 一等参数，比把 site: 拼进查询串稳
            body["search_domain_filter"] = site
        self.calls += 1
        self._throttle(urlparse(self.endpoint).hostname or "")
        for attempt in range(MAX_RETRIES + 1):
            try:
                r = httpx.post(
                    self.endpoint, json=body,
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json", "User-Agent": UA},
                    timeout=self.timeout,
                )
                if r.status_code in (401, 402, 403):
                    raise SearchUnavailable(f"智谱搜索返回 {r.status_code}：{r.text[:200]}")
                if r.status_code == 429:
                    # 智谱把「余额不足 / 无资源包」也放在 429 下。它不是稍后重试就会
                    # 恢复的限流；此前这里连续重试后返回 []，界面只表现为“什么都搜不到”。
                    message = r.text[:300]
                    try:
                        err = r.json().get("error") or {}
                        code = str(err.get("code") or "")
                        message = str(err.get("message") or message)
                    except Exception:
                        code = ""
                    if code == "1113" or any(x in message for x in ("余额不足", "资源包", "充值")):
                        raise SearchUnavailable(f"智谱搜索不可用：{message}")
                    # 普通限流（多条陈述并行时可能碰到）：退避久一点再试，别马上判"搜索不可用"。
                    if attempt < MAX_RETRIES:
                        time.sleep(2.0 * (attempt + 1))
                        continue
                if is_content_filtered(r):
                    # 不重发同一串：审核判决不是网络抖动，一模一样的字面重试毫无意义，
                    # 只会白烧时间。改述是调用方的决定（它才知道这条查询值不值得救）。
                    raise SearchFiltered(
                        f"查询被内容审核拒绝（code 1301）：{query[:80]}", query=query, site=site)
                r.raise_for_status()
                hits = self._parse(r.json())
                return [h for h in hits if url_in_domain(h.url, site)] if site else hits
            except SearchUnavailable:
                raise
            except Exception as exc:
                if attempt >= MAX_RETRIES:
                    raise SearchUnavailable(
                        f"智谱搜索请求失败（{type(exc).__name__}），已重试 {MAX_RETRIES} 次"
                    ) from exc
                time.sleep(0.5 * (attempt + 1))
        return []

    def ping(self) -> list[SearchHit]:
        return self.search("清华大学 计算机系 公示", site="tsinghua.edu.cn")


class HTTPSearcher:
    """通用 HTTP 搜索适配器。

    ⚠️ 尚未对接任何具体供应商：SEARCH_ENDPOINT / SEARCH_API_KEY 由部署方给出，
    响应字段映射在 _parse 里改一处即可。没配端点时行为与 NullSearcher 一致。
    """

    def __init__(self, endpoint: str | None = None, api_key: str | None = None, timeout: float = 15.0):
        self.endpoint = endpoint or os.environ.get("SEARCH_ENDPOINT", "")
        self.api_key = api_key or os.environ.get("SEARCH_API_KEY", "")
        self.timeout = timeout
        self._last_call: dict[str, float] = {}
        self._throttle_lock = threading.Lock()

    def _throttle(self, host: str) -> None:
        with self._throttle_lock:
            last = self._last_call.get(host, 0.0)
            wait = search_interval() - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
            self._last_call[host] = time.monotonic()

    @staticmethod
    def _parse(payload: dict) -> list[SearchHit]:
        items = payload.get("results") or payload.get("items") or payload.get("webPages", {}).get("value", [])
        out = []
        for it in items:
            url = it.get("url") or it.get("link") or ""
            if not url:
                continue
            out.append(SearchHit(
                url=url,
                title=it.get("title") or it.get("name") or "",
                snippet=it.get("snippet") or it.get("description") or "",
                publisher=it.get("source") or "",
            ))
        return out

    def search(self, query: str, site: str | None = None) -> list[SearchHit]:
        if not self.endpoint or not self.api_key:
            return []
        import httpx
        q = f"site:{site} {query}" if site else query
        self._throttle(urlparse(self.endpoint).hostname or "")
        for attempt in range(MAX_RETRIES + 1):
            try:
                r = httpx.get(
                    self.endpoint,
                    params={"q": q},
                    headers={"User-Agent": UA, "Authorization": f"Bearer {self.api_key}"},
                    timeout=self.timeout,
                )
                if r.status_code in (401, 402, 403):
                    raise SearchUnavailable(f"搜索服务返回 {r.status_code}：{r.text[:200]}")
                r.raise_for_status()
                return self._parse(r.json())
            except SearchUnavailable:
                raise
            except Exception as exc:
                if attempt >= MAX_RETRIES:
                    raise SearchUnavailable(
                        f"搜索请求失败（{type(exc).__name__}），已重试 {MAX_RETRIES} 次"
                    ) from exc
                time.sleep(0.5 * (attempt + 1))
        return []


class BraveSearcher:
    """可选备用索引：Brave Web Search，配置 BRAVE_SEARCH_API_KEY 后启用。"""

    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str = "", timeout: float = 15.0):
        self.api_key = api_key or os.environ.get("BRAVE_SEARCH_API_KEY", "")
        self.timeout = timeout
        self._last = 0.0
        self._lock = threading.Lock()

    def search(self, query: str, site: str | None = None) -> list[SearchHit]:
        if not self.api_key:
            return []
        import httpx
        with self._lock:
            wait = RATE_LIMIT_SECONDS - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        q = f"site:{site} {query}" if site else query
        try:
            response = httpx.get(
                self.ENDPOINT,
                params={"q": q, "count": 10},
                headers={"X-Subscription-Token": self.api_key,
                         "Accept": "application/json", "User-Agent": UA},
                timeout=self.timeout,
            )
            if response.status_code in (401, 402, 403, 429):
                raise SearchUnavailable(f"Brave 搜索不可用：HTTP {response.status_code}")
            response.raise_for_status()
            results = ((response.json().get("web") or {}).get("results") or [])
        except SearchUnavailable:
            raise
        except Exception as exc:
            raise SearchUnavailable(f"Brave 搜索请求失败（{type(exc).__name__}）") from exc
        hits = [SearchHit(url=item.get("url") or "", title=item.get("title") or "",
                          snippet=item.get("description") or "") for item in results]
        return [hit for hit in hits if url_in_domain(hit.url, site)] if site else [
            hit for hit in hits if hit.url.startswith(("https://", "http://"))]


class FallbackSearcher:
    """主索引空结果或不可用时使用备用索引；不把双失败伪装成零结果。"""

    def __init__(self, primary: Searcher, secondary: Searcher):
        self.primary = primary
        self.secondary = secondary

    def search(self, query: str, site: str | None = None) -> list[SearchHit]:
        failure: SearchUnavailable | None = None
        try:
            hits = self.primary.search(query, site=site)
            if hits:
                return hits
        except SearchUnavailable as exc:
            failure = exc
        try:
            return self.secondary.search(query, site=site)
        except SearchUnavailable:
            if failure:
                raise failure
            raise

    def confirm_empty(self, query: str, site: str | None = None) -> list[SearchHit]:
        confirm = getattr(self.primary, "confirm_empty", None)
        return confirm(query, site=site) if callable(confirm) else []


# --------------------------------------------------------------------------
# 页面读取
# --------------------------------------------------------------------------

_robots_cache: dict[str, robotparser.RobotFileParser | None] = {}
_robots_guard = threading.Lock()
ROBOTS_TIMEOUT_SECONDS = 5.0


def _load_robots(root: str) -> robotparser.RobotFileParser | None:
    """用本项目的 UA 和短超时取 robots.txt。

    以前直接调 `RobotFileParser.read()`，有两个实测会出事的行为：
      1. 它走 urllib，**没有超时**。学校站点不回应时，一次页面读取会卡到系统 TCP
         超时（macOS 上一分多钟），整条陈述的时间预算被它一口吃光，后面的名单
         查询根本没机会发出——日志里只看到"回读失败/未完成"和"已触及预算"。
      2. 它用 `Python-urllib` 的 UA，很多高校 WAF 对这个 UA 返回 403；而标准库把
         401/403 解释成 **disallow_all**，于是整个站点的公示和附件都被判成
         "robots 不允许"。这和"站长禁止抓取"完全是两回事。
    现在按主流爬虫的口径：200 才解析规则；4xx（含 401/403/404）视为没有规则；
    超时、连接失败、5xx 也按允许处理（与原注释"取不到时按允许"一致）。
    """
    import httpx
    try:
        r = httpx.get(root + "/robots.txt", headers={"User-Agent": UA},
                      timeout=ROBOTS_TIMEOUT_SECONDS, follow_redirects=True)
    except Exception:
        return None
    if r.status_code != 200:
        return None
    ctype = r.headers.get("content-type", "").lower()
    # 有些站点对不存在的 robots.txt 返回 200 + 首页 HTML，不能当规则解析。
    if "html" in ctype or r.text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        return None
    rp = robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    return rp


def robots_allows(url: str, ua: str = UA) -> bool:
    """遵守 robots.txt；取不到 robots.txt 时按允许处理（与主流爬虫一致）。"""
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    with _robots_guard:
        cached = root in _robots_cache
    if not cached:
        rp = _load_robots(root)
        with _robots_guard:
            _robots_cache[root] = rp
    rp = _robots_cache[root]
    return True if rp is None else rp.can_fetch(ua, url)


def github_hits(entities: dict, limit: int = 4) -> list[SearchHit]:
    """候选人简历里给出的 GitHub 账号/仓库 → 直接走公共 API，**不经过搜索引擎**。

    这是一等锚点：链接是本人自己写在简历里的，不存在同名误认。
    GitHub 的个人/仓库页是 JS 渲染的，静态抓取拿不到内容——但公共 API 无需登录。
    """
    import httpx
    handle = (entities or {}).get("github") or ""
    repo = ((entities or {}).get("repo") or "").strip()
    if not handle and "/" not in repo:
        return []
    out: list[SearchHit] = []
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    try:
        if handle:
            r = httpx.get(f"https://api.github.com/users/{handle}",
                          headers=headers, timeout=15.0)
            if r.status_code == 200:
                u = r.json()
                snippet = (f"GitHub 用户：{u.get('login', '')}；"
                           f"姓名：{u.get('name') or '（未填写）'}；"
                           f"公开仓库：{u.get('public_repos', 0)} 个；"
                           f"简介：{u.get('bio') or '（无）'}；"
                           f"注册于：{(u.get('created_at') or '')[:10]}")
                out.append(SearchHit(
                    url=u.get("html_url", ""),
                    title=f"GitHub 主页 · {u.get('login', '')}",
                    snippet=snippet, publisher="github.com", content=snippet))
        if repo and "/" in repo:
            r = httpx.get(f"https://api.github.com/repos/{repo}",
                          headers=headers, timeout=15.0)
            if r.status_code == 200:
                u = r.json()
                snippet = (f"GitHub 仓库：{u.get('full_name', '')}；"
                           f"描述：{u.get('description') or '（无描述）'}；"
                           f"主要语言：{u.get('language') or '—'}；"
                           f"Stars：{u.get('stargazers_count', 0)}；"
                           f"更新于：{(u.get('pushed_at') or '')[:10]}")
                out.append(SearchHit(
                    url=u.get("html_url", ""),
                    title=f"GitHub 仓库 · {u.get('full_name', '')}",
                    snippet=snippet, publisher="github.com", content=snippet))
    except Exception:
        return out
    return out[:limit]


def crossref_hits(title: str, limit: int = 5) -> list[SearchHit]:
    """按论文标题查 Crossref，并把结构化元数据直接交给证据抽取层。

    原先策略把查询标成 ``kind=crossref``，执行层却仍然调用普通网页搜索，kind
    完全没有生效。直连公开 API 后，论文标题、作者顺序、期刊和 DOI 都能稳定取回。
    """
    import httpx
    from difflib import SequenceMatcher

    query = (title or "").strip().strip('"').strip()
    if not query:
        return []
    headers = {"User-Agent": UA, "Accept": "application/json"}
    try:
        r = httpx.get(
            "https://api.crossref.org/works",
            params={
                "query.title": query,
                # 多取一点再在本地按标题相似度重排，避免同名扩展标题挤掉精确匹配。
                "rows": max(10, min(int(limit) * 2, 20)),
                "select": "DOI,title,author,published,container-title,publisher,type,link,resource",
            },
            headers=headers,
            timeout=20.0,
        )
        r.raise_for_status()
        items = ((r.json().get("message") or {}).get("items") or [])
    except Exception:
        return []

    def norm(value: str) -> str:
        return "".join(re.findall(r"[a-z0-9\u3400-\u9fff]+", (value or "").lower()))

    wanted = norm(query)
    items.sort(
        key=lambda it: (
            norm(" ".join(it.get("title") or [])) != wanted,
            -SequenceMatcher(None, wanted, norm(" ".join(it.get("title") or []))).ratio(),
        )
    )

    out = [hit for hit in (_crossref_hit(it) for it in items) if hit]
    return out[:max(1, int(limit))]


def _crossref_hit(it: dict) -> SearchHit | None:
    """Crossref 的一条 work 记录 → SearchHit（标题查询与 DOI 精确查询共用）。"""
    paper_title = " ".join(it.get("title") or []).strip()
    doi = (it.get("DOI") or "").strip()
    if not paper_title or not doi:
        return None
    authors = []
    for a in it.get("author") or []:
        name = " ".join(x for x in (a.get("given"), a.get("family")) if x).strip()
        if name:
            authors.append(name)
    venue = " ".join(it.get("container-title") or []).strip()
    parts = ((it.get("published") or {}).get("date-parts") or [[]])[0]
    published = "-".join(str(x) for x in parts) if parts else ""
    content = (
        f"Crossref 元数据\n题名：{paper_title}\n"
        f"作者（按元数据顺序）：{'; '.join(authors) or '（未提供）'}\n"
        f"刊物或会议：{venue or '（未提供）'}\n"
        f"出版方：{it.get('publisher') or '（未提供）'}\n"
        f"发表日期：{published or '（未提供）'}\nDOI：{doi}"
    )
    links = [((it.get("resource") or {}).get("primary") or {}).get("URL") or ""]
    links.extend(link.get("URL") or "" for link in (it.get("link") or []))
    official_url = next((u for u in links if u.startswith(("https://", "http://"))
                         and not url_in_domain(u, "doi.org")), "")
    return SearchHit(
        url=f"https://doi.org/{doi}", title=paper_title,
        snippet=content.replace("\n", "；"),
        publisher=it.get("publisher") or "Crossref", content=content,
        official_url=official_url, authors=tuple(authors), venue=venue,
        year=str(parts[0]) if parts else "",
    )


def crossref_doi_hit(doi: str) -> SearchHit | None:
    """简历写了 DOI 时按 DOI 精确取元数据。标题检索偶尔会被同名长标题挤掉，DOI 不会。"""
    import httpx
    from urllib.parse import quote

    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", (doi or "").strip(), flags=re.I)
    if not re.match(r"10\.\d{4,9}/\S+", value):
        return None
    try:
        r = httpx.get(f"https://api.crossref.org/works/{quote(value, safe='/')}",
                      headers={"User-Agent": UA, "Accept": "application/json"}, timeout=20.0)
        if r.status_code != 200:
            return None
        return _crossref_hit((r.json() or {}).get("message") or {})
    except Exception:
        return None


# 专利的免费检索面：中文专利没有可直接调用的免费公开 API——CNIPA 的公众查询系统
# 要登录和验证码，EPO OPS / Lens 都要申请 key，PatentsView 只覆盖美国专利。
# 退而求其次：Google Patents 收录了 CN 公开文本（含中文题名与发明人），页面可静态读，
# 所以把专利查询**限定到这个域**而不是裸搜全网。好处是双份的：
# 裸查询走的是最贵的 search_pro_sogou（0.05/次），限定域后走 search_std（0.01/次），
# 而且精度高得多——裸搜"张三 某某装置"几乎必然捞回一堆无关网页。
PATENT_SITE = "patents.google.com"


def openalex_hits(title: str, limit: int = 5) -> list[SearchHit]:
    """按论文标题查 OpenAlex。免费、无需 key，和 Crossref 互补。

    为什么不只用 Crossref：Crossref 只认领了 DOI 的记录，中文期刊、会议论文集和
    学位论文经常查不到；OpenAlex 把这些也收了，而且直接给作者列表和机构，
    正好是判定"是不是本人"要用的东西。两边都免费，所以一起查、按标题去重。
    """
    import httpx
    from difflib import SequenceMatcher

    query = (title or "").strip().strip('"').strip()
    if not query:
        return []
    try:
        r = httpx.get(
            "https://api.openalex.org/works",
            # mailto 进 polite pool：不是鉴权，是 OpenAlex 要求的礼貌标识，配额更宽。
            params={"search": query, "per-page": max(5, min(int(limit) * 2, 20)),
                    "mailto": "under-box@example.invalid"},
            headers={"User-Agent": UA, "Accept": "application/json"},
            timeout=20.0,
        )
        r.raise_for_status()
        items = r.json().get("results") or []
    except Exception:
        return []

    def norm(value: str) -> str:
        return "".join(re.findall(r"[a-z0-9\u3400-\u9fff]+", (value or "").lower()))

    wanted = norm(query)
    items.sort(key=lambda it: -SequenceMatcher(
        None, wanted, norm(it.get("display_name") or "")).ratio())

    out: list[SearchHit] = []
    for it in items:
        paper_title = (it.get("display_name") or "").strip()
        if not paper_title:
            continue
        authors = []
        for a in it.get("authorships") or []:
            name = ((a.get("author") or {}).get("display_name") or "").strip()
            if name:
                insts = "、".join(
                    (i.get("display_name") or "") for i in (a.get("institutions") or []))
                authors.append(f"{name}（{insts}）" if insts else name)
        loc = (it.get("primary_location") or {}).get("source") or {}
        venue = (loc.get("display_name") or "").strip()
        landing = ((it.get("primary_location") or {}).get("landing_page_url") or "").strip()
        doi = (it.get("doi") or "").strip()
        url = doi or it.get("id") or ""
        content = (
            f"OpenAlex 元数据\n题名：{paper_title}\n"
            f"作者（按元数据顺序）：{'; '.join(authors) or '（未提供）'}\n"
            f"刊物或会议：{venue or '（未提供）'}\n"
            f"发表年份：{it.get('publication_year') or '（未提供）'}\n"
            f"被引次数：{it.get('cited_by_count', 0)}\nDOI：{doi or '（无）'}"
        )
        if not url:
            continue
        out.append(SearchHit(
            url=url, title=paper_title, snippet=content.replace("\n", "；"),
            publisher=venue or "OpenAlex", content=content,
            official_url=landing if landing.startswith(("https://", "http://")) else "",
            authors=tuple(authors), venue=venue,
            year=str(it.get("publication_year") or "")))
    return out[:max(1, int(limit))]


def paper_hits(title: str, limit: int = 5, *, author: str = "", venue: str = "",
               year: str = "", doi: str = "") -> list[SearchHit]:
    """论文陈述的免费取证入口：Crossref + OpenAlex，按 DOI 去重并结合陈述排序。

    两个都是公开 API，不消耗搜索额度。Crossref 排前面——它的出版方和
    DOI 更权威；OpenAlex 补上没有 DOI 的中文论文与会议论文。
    """
    def norm(value: str) -> str:
        return "".join(re.findall(r"[a-z0-9\u3400-\u9fff]+", (value or "").lower()))

    out: list[SearchHit] = []
    positions: dict[str, int] = {}
    # 两个独立的元数据站并发请求；结果仍按 Crossref → OpenAlex 固定顺序合并。
    # 简历写了 DOI 时再并发一路 DOI 精确查询，排在最前；它同样要过下面的标题相似度门槛——
    # DOI 写错（指向别的论文）时不能拿别人的论文当证据。
    with ThreadPoolExecutor(max_workers=3) as pool:
        crossref_future = pool.submit(crossref_hits, title, limit)
        openalex_future = pool.submit(openalex_hits, title, limit)
        doi_future = pool.submit(crossref_doi_hit, doi) if doi else None
        crossref = crossref_future.result()
        openalex = openalex_future.result()
        by_doi = doi_future.result() if doi_future else None
    for hit in [*([by_doi] if by_doi else []), *crossref, *openalex]:
        key = hit.url.lower().rstrip("/")
        if key in positions:
            previous = out[positions[key]]
            if not previous.official_url and hit.official_url:
                previous.official_url = hit.official_url
            continue
        positions[key] = len(out)
        out.append(hit)

    wanted = norm(title)
    requested_year = re.search(r"(?:19|20)\d{2}", year or "")
    if requested_year:
        # 明显不在简历所写年份附近的同名作品不是这条陈述的候选证据。
        out = [hit for hit in out if not hit.year or not hit.year.isdigit()
               or abs(int(hit.year) - int(requested_year.group())) <= 1]

    author_parts = [norm(part) for part in re.split(r"\s+", author or "") if part]
    venue_key = norm(venue)

    def relevance(hit: SearchHit) -> tuple[float, int, int, int]:
        from difflib import SequenceMatcher
        title_score = SequenceMatcher(None, wanted, norm(hit.title)).ratio()
        author_blob = norm(" ".join(hit.authors))
        author_score = sum(1 for part in author_parts if len(part) >= 2 and part in author_blob)
        known_site = 1 if hit.official_url and not url_in_domain(hit.official_url, "doi.org") else 0
        venue_score = 1 if venue_key and venue_key in norm(hit.venue) else 0
        return (title_score, author_score, venue_score, known_site)

    out = [hit for hit in out if relevance(hit)[0] >= 0.75]
    out.sort(key=relevance, reverse=True)
    return out[:max(1, int(limit))]


_META_CHARSET = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""", re.I)


def decode_html(r) -> str:
    """按响应头 → <meta charset> → UTF-8 → GB18030 的顺序解码。

    不少学校网站是 GBK/GB2312，但响应头不写 charset；httpx 这时默认按 UTF-8 解，
    中文全成乱码（2026-10 批量发现：大连工业大学首页标题解出来是"������ҵ��ѧ"，
    名单里的姓名也就匹配不上）。
    """
    content = getattr(r, "content", b"") or b""
    if not isinstance(content, (bytes, bytearray)):
        return getattr(r, "text", "") or ""
    headers = getattr(r, "headers", {}) or {}
    ctype = (headers.get("content-type", "") if hasattr(headers, "get") else "").lower()
    declared = None
    m = re.search(r"charset=([\w\-]+)", ctype)
    if m:
        declared = m.group(1)
    else:
        m = _META_CHARSET.search(content[:4096])
        if m:
            declared = m.group(1).decode("ascii", "ignore")
    if declared:
        d = declared.lower()
        enc = "gb18030" if d in ("gb2312", "gbk", "gb18030", "x-gbk", "cp936") else d
        try:
            return content.decode(enc, errors="replace")   # 写明了编码就信它，个别坏字节替换掉
        except LookupError:
            pass
    for enc in ("utf-8", "gb18030"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


class PageFetcher:
    """单域名限流 + robots 检查 + 有限重试的页面读取。

    超时口径（2026-10 调整）：连接 5 秒、读取 12 秒，只对连接失败/5xx 重试一次。
    原来是 20 秒 × 最多 3 次，一个不回应的页面最长能占掉一分钟，每条陈述只有
    90–180 秒，读两三页失败就把后面的名单查询全部饿死。读超时重试几乎从不成功，
    还要再等 12 秒，所以读超时不再重试。
    """

    PAGE_RETRIES = 1

    def __init__(self, timeout: float = 12.0, connect_timeout: float = 5.0):
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._last: dict[str, float] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.failures: dict[str, str] = {}
        # 附件相对路径必须以重定向后的页面地址为基准，例如 results → results/。
        self.resolved_urls: dict[str, str] = {}
        # 连不上的站点（连接超时/拒绝）本次核验内不再重试：同一学院站点往往一次
        # 搜出好几条链接，每条都等满连接超时，两三个站点就能吃掉整条陈述的时间。
        self.dead_hosts: dict[str, str] = {}

    def _host_lock(self, host: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(host, threading.Lock())

    def _throttle(self, host: str) -> None:
        wait = RATE_LIMIT_SECONDS - (time.monotonic() - self._last.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        self._last[host] = time.monotonic()

    def get_roster(self, url: str, claim) -> str | None:
        from .competitions import competition_for_claim
        from .plan import claim_school, school_domain
        item = competition_for_claim(claim) or {}
        school = school_domain(claim_school(claim) or (claim.entities or {}).get("school_search_hint", ""))
        hints = [hint for p in item.get("school_result_pages", []) if p.get("school_domain") == school
                 for hint in p.get("archive_hints", [])]
        # 学校所在地只影响包内文件读取优先级，不据此声称候选人的参赛省份。
        return self.get(url, archive_hint=str(claim.raw_text) + str(claim.entities) + " ".join(hints))

    def get(self, url: str, *, archive_hint: str = "") -> str | None:
        import httpx
        host = urlparse(url).hostname or ""
        # 不同域可以并发；同一域仍严格串行并保持至少 1 秒间隔。
        # 锁包住 robots 检查与重试，避免多个线程同时冲击同一站点。
        with self._host_lock(host):
            self.failures.pop(url, None)
            dead_reason = self.dead_hosts.get(host) or self.dead_hosts.get(f"{urlparse(url).scheme}://{host}")
            if dead_reason:
                self.failures[url] = f"同一站点刚才{dead_reason}，已跳过"
                return None
            if not robots_allows(url):
                self.failures[url] = "robots.txt 不允许访问"
                return None
            self._throttle(host)
            timeout = httpx.Timeout(self.timeout, connect=self.connect_timeout)
            for attempt in range(self.PAGE_RETRIES + 1):
                try:
                    r = httpx.get(url, headers={"User-Agent": UA}, timeout=timeout,
                                  follow_redirects=True)
                    status = getattr(r, "status_code", 200)
                    if isinstance(status, int) and 400 <= status < 500:
                        # 404/403 之类重试也不会变，直接记原因，别再等。
                        self.failures[url] = f"HTTP {status}"
                        return None
                    r.raise_for_status()
                    resolved = str(getattr(r, "url", "") or "")
                    if urlparse(resolved).scheme in ("http", "https"):
                        self.resolved_urls[url] = resolved
                    if r.content[:1024].lstrip().startswith(b"%PDF-"):
                        if len(r.content) > MAX_PUBLIC_PDF_BYTES:
                            self.failures[url] = "PDF 超过公开名单读取上限（32 MB）"
                            return None
                        text = _pdf_text(r.content)
                        if not text:
                            self.failures[url] = "PDF 未提取到文本，可能是扫描件或受保护文件"
                        return text
                    if r.content.startswith(b"PK\x03\x04"):
                        text = _office_text(r.content)
                        if not text:
                            from .roster_archive import archive_text
                            text = archive_text(r.content,hint=archive_hint,pdf_parser=_pdf_text,office_parser=_office_text)
                        if not text:
                            self.failures[url] = "DOCX/XLSX 未提取到文本或文件格式不支持"
                        return text
                    if r.content.startswith(b"Rar!\x1a\x07"):
                        from .roster_archive import archive_text
                        text = archive_text(r.content,hint=archive_hint,pdf_parser=_pdf_text,office_parser=_office_text)
                        if not text:
                            self.failures[url] = "RAR名单包未读取到文档（本机解压工具不可用、超限或文件无文本）"
                        return text
                    if "application/pdf" in r.headers.get("content-type", "").lower():
                        self.failures[url] = "返回内容不是有效 PDF"
                        return None
                    if r.content.startswith(b"\xd0\xcf\x11\xe0"):
                        # 旧版 DOC/XLS 不作二进制文本解码。
                        self.failures[url] = "暂不支持旧版 DOC/XLS 文件"
                        return None
                    if (r.content.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff"))
                            or r.content[:4] == b"RIFF" and r.content[8:12] == b"WEBP"):
                        from .ocr import recognize_image
                        text = recognize_image(r.content)
                        if not text:
                            self.failures[url] = "名单图片OCR未读取到可靠文本（本机OCR不可用、超时或图片质量不足）"
                        return text
                    text = decode_html(r)
                    if any(term in text for term in ("请输入验证码下载附件", "验证码下载",
                                                     "请登录后下载", "您没有权限下载")):
                        self.failures[url] = "附件下载需要验证码、登录或额外权限"
                        return None
                    # 官网公开公告正文由前端GET接口加载；只读固定公开接口，不执行JS。
                    notice = re.fullmatch(r"/notices/(\d+)/?", urlparse(url).path)
                    if host == "dasai.lanqiao.cn" and notice and "<h1" not in text.lower():
                        import json
                        from html import escape
                        api = "https://www.guoxinlanqiao.com/api/web/news/selectone?nnid=" + notice.group(1)
                        payload = self.get(api)
                        try:
                            news = json.loads(payload or "{}").get("news") or {}
                            title, body = news.get("title"), news.get("content")
                            if isinstance(title, str) and isinstance(body, str) and body.strip():
                                return f"<html><title>{escape(title)}</title><article><h1>{escape(title)}</h1>" + body + "</article></html>"
                        except (ValueError, AttributeError):
                            pass
                        self.failures[url] = "蓝桥杯动态公告正文未能读取；页面空壳不等于没有名单"
                        return None
                    return text
                except httpx.ReadTimeout:
                    self.failures[url] = f"读取超时（>{self.timeout:.0f} 秒）"
                    return None
                except httpx.ConnectTimeout:
                    reason = f"连接超时（>{self.connect_timeout:.0f} 秒）"
                except httpx.ConnectError as exc:
                    text = str(exc)
                    reason = ("证书校验失败（SSL）" if "CERTIFICATE" in text.upper() or "SSL" in text.upper()
                              else "连接失败")
                    if reason == "证书校验失败（SSL）":
                        if "expired" in text.lower():
                            reason = "网站证书已过期（SSL）"
                        # 同次核验再访问同域的十条公告也无法修复证书；保留原因并节省等待。
                        self.failures[url] = reason
                        self.dead_hosts[f"https://{host}"] = reason
                        return None
                except httpx.HTTPStatusError as exc:
                    reason = f"HTTP {exc.response.status_code}"
                except Exception as exc:
                    reason = f"请求失败（{type(exc).__name__}）"
                if attempt >= self.PAGE_RETRIES:
                    self.failures[url] = reason
                    if reason.startswith(("连接超时", "连接失败")):
                        self.dead_hosts[host] = reason
                    return None
                time.sleep(0.5 * (attempt + 1))
        return None


SEARCH_PROVIDERS = {"zhipu": ZhipuSearcher, "generic": HTTPSearcher,
                    "brave": BraveSearcher}


def build_searcher(provider: str | None = None) -> Searcher:
    """按环境变量装配。默认智谱。

    SEARCH_API_KEY=...                 # 智谱主索引
    BRAVE_SEARCH_API_KEY=...           # 可选；主索引空结果或故障时使用
    SEARCH_ENGINE=search_std           # 可选，带域限定的查询用；默认 std（0.01/次）
    SEARCH_ENGINE_FALLBACK=search_pro  # 可选，std 空结果时再确认一次（默认关）
    SEARCH_ENGINE_OPEN=search_pro_sogou  # 可选，裸查询用；只有它返回 link
    SEARCH_ENDPOINT=...                # 可选：端点有变时覆盖
    SEARCH_PROVIDER=zhipu|brave|generic # 可选
    """
    name = (provider or os.environ.get("SEARCH_PROVIDER") or "zhipu").lower()
    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if name == "brave":
        return BraveSearcher() if brave_key else NullSearcher()
    if not os.environ.get("SEARCH_API_KEY"):
        return BraveSearcher() if brave_key else NullSearcher()
    primary = SEARCH_PROVIDERS.get(name, ZhipuSearcher)()
    return FallbackSearcher(primary, BraveSearcher()) if brave_key else primary
