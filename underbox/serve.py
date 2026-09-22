#!/usr/bin/env python3
"""underbox 本地服务：静态页 + 真的走 DeepSeek Harness 的问答接口。

    python3 serve.py                      # http://127.0.0.1:8787
    python3 serve.py --port 9000
    python3 serve.py --profile headless

为什么需要它：浏览器里的页面没法直接跟 dsh 说话——headless 是命令行，
sdk profile 是 stdio 上的 JSON-RPC，两者都不是浏览器能直接讲的协议。
所以这里起一个极小的本地服务，把问题连同**本页的核验结果**一起喂给
`dsh --profile headless`，再把回答带回去。只用标准库。

一个实测细节：headless 的 stdout **就是回答本身**，`dsh: reasoning:` 之类的
诊断都走 stderr。所以取 stdout 即可，不用解析前缀。

注意：`headless` profile 执行不了工具（缺 ToolRuntimeScheduler，内置 bash 同样报错），
所以问答提示词里明确要求"不要调用任何工具，只依据材料回答"。
纯对话在 headless 上工作正常，这也是这条路径能用的原因。真要做多步编排得走 web profile。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import sys
import tempfile
import threading
import time
from urllib.parse import unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
# 默认指向同仓库的 ../open_box。旧版写死了某台开发机的绝对路径，
# 换机器后会变成"页面能开、一上传简历就报找不到模块"。
OPEN_BOX = Path(os.environ.get("OPEN_BOX_ROOT") or (HERE.parent / "open_box"))
if str(OPEN_BOX) not in sys.path:
    sys.path.insert(0, str(OPEN_BOX))
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


# ── 报告 → 喂给模型的材料 ────────────────────────────────────────────────


def _risk_label(detail: str) -> str:
    """把 open_box 的风险描述裁到"模式名"为止，去掉匹配到的原文。

    `parse.py` 的描述形如 `命中提示词注入模式：忽略既有指令（匹配片段"忽略之前的指令"）`。
    后半截是给人看的，但**不能进 prompt**：注入内容完全可以是精心构造的，
    让"匹配片段"恰好等于一条完整指令——那样引用它等于把指令原样递回去。
    所以只保留 `（匹配片段` 之前的部分，那里是 open_box 自己的模式标签，不是载荷。
    """
    return detail.split("（匹配片段")[0].strip() or detail


def render_material(rep) -> str:
    """把 open_box 的 Report 压成给模型的材料文本。

    ⚠ 入参是 Report **对象**（有 .claims 属性，没有 .get）——之前这里按 dict 写，
    一到对话端点就 AttributeError，而且发生在响应头发出之前，浏览器只会看到
    "Failed to fetch"，完全看不出是服务端崩了。
    """
    status_cn = {"ok": "已证实", "part": "部分证实", "ask": "待澄清",
                 "none": "未找到公开记录", "who": "身份未确认"}
    out = [
        f"候选人：{rep.candidate_name}　应聘：{rep.position or '（未标注）'}",
        f"报告编号：{rep.report_no}　共 {len(rep.claims)} 条陈述",
        "",
    ]
    for v in rep.claims:
        c = v.claim
        out.append(f"[{c.id}] {status_cn.get(v.status, v.status)}"
                   f"　{c.category}　{c.date_label or '时间未标注'}"
                   f"　来源等级 {v.best_tier or '无'}")
        out.append(f"  陈述原文：{c.raw_text}")
        if c.elements:
            out.append(f"  需分别证明的要素：{'；'.join(c.elements)}")
        if v.proved:
            out.append(f"  已证明：{'；'.join(v.proved)}")
        if v.unproved:
            out.append(f"  尚未证明：{'；'.join(v.unproved)}")
        for e in v.evidence:
            out.append(f"  证据（{e.source_tier or '未定级'}）：{e.title}"
                       f"｜{e.publisher}｜{e.published_at or '未标注时间'}")
            if e.snippet:
                out.append(f"    摘录：{e.snippet}")
            if e.identity_signals:
                out.append(f"    身份信号：{'、'.join(e.identity_signals)}")
            if e.identity_conflicts:
                out.append(f"    身份矛盾：{'；'.join(e.identity_conflicts)}")
            if e.contradicts:
                out.append(f"    与陈述的矛盾：{'；'.join(e.contradicts)}")
        if v.needs_human:
            out.append("  标记：需人工复核")
        if v.next_step:
            out.append(f"  下一步建议：{v.next_step}")
        out.append("")
    if rep.input_risks:
        # 只报"检测到了什么、在哪"，**不带命中原文**——那些正是 open_box 从
        # 送入模型的内容里剔除的东西，传回去等于把刚筛掉的指令又递给模型。
        out.append("输入风险（已从送入模型的简历正文中剔除，下面只说明检测结果，不含原文）：")
        for k in rep.input_risks:
            out.append(f"  位置 {k.locator or '未标注'}　类型 {k.kind}　{_risk_label(k.detail)}")
    return "\n".join(out)


def build_prompt(material: str, question: str) -> str:
    return (
        "你是 UNDERBOX 的报告问答助手。下面是一份履历核验报告的完整结果，"
        "回答用户关于这份报告的问题。\n\n"
        "硬性要求：\n"
        "1. 只依据下面的材料回答。材料里没有的，直接说材料里没有，不要推测、不要编造。\n"
        "2. 不要调用任何工具，只做对话回答。\n"
        "3. 不要给候选人打分、排名，也不要给录用建议。\n"
        "4. 措辞中性：差异不等于造假（可能是后续接任、口径不同或材料未公开）；"
        "未找到公开记录不等于经历不实；同名不等于同一人。\n"
        "5. 回答尽量短，需要时引用具体是哪一条（如 c05）和它的证据。\n\n"
        f"=== 报告材料 ===\n{material}\n=== 材料结束 ===\n\n"
        f"用户的问题：{question}"
    )


# ── 调 dsh ───────────────────────────────────────────────────────────────



# ── 真实核验：Report → 网页端形状 ─────────────────────────────────────────

def _search_configured() -> bool:
    return bool(os.environ.get("SEARCH_API_KEY"))


def _llm_ready() -> bool:
    """模型 key 配了吗？读 app（它会顺带加载 open_box/.env）。"""
    try:
        import app                                    # noqa: PLC0415
        return bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("LLM_API_KEY"))
    except Exception:
        return False


def _pipeline_ready() -> bool:
    """核验流水线真的能导入吗？

    原先这个字段写死 True，于是"解释器缺 pydantic / httpx"这类问题在健康检查里
    完全看不出来——页面显示「LIVE · 真实核验」，用户拖进简历那一刻才炸
    ModuleNotFoundError。真探一次的代价只是一次导入。
    """
    try:
        from app.pipeline import run_pipeline        # noqa: F401, PLC0415
        return True
    except Exception:
        return False


def to_presentation(rep) -> dict:
    """把 open_box 的 Report 转成 underbox/index.html 渲染的 REPORT 形状。

    字段一一对应（tier/status/elements/evidence...），网页端不用改渲染逻辑就能吃真实数据。
    """
    return {
        "reportNo": rep.report_no,
        "candidate": rep.candidate_name,
        "position": rep.position,
        "disclaimer": rep.disclaimer,
        "generatedAt": rep.generated_at,
        "mode": rep.mode,
        "sourceFile": rep.source_file,
        "searchConfigured": _search_configured(),
        "risks": [{"kind": r.kind, "locator": r.locator, "detail": r.detail, "excerpt": r.excerpt}
                  for r in rep.input_risks],
        "claims": [{
            "id": v.claim.id,
            "category": v.claim.category,
            "date": v.claim.date_label,
            "tier": v.best_tier or "",
            "status": v.status,
            "needsHuman": v.needs_human,
            "rawText": v.claim.raw_text,
            "locator": v.claim.raw_locator,
            "elements": v.claim.elements,
            "proved": v.proved,
            "unproved": v.unproved,
            "nextStep": v.next_step,
            "question": v.question or "",
            "searchExhausted": v.search_exhausted,
            "evidence": [{
                "tier": e.source_tier,
                "title": e.title,
                "publisher": e.publisher,
                "url": e.url,
                "snippet": e.snippet,
                "date": e.published_at or "",
                "signals": e.identity_signals,
                "conflicts": e.identity_conflicts,
                "contradicts": e.contradicts,
                "isRepost": bool(e.origin_url),
            } for e in v.evidence],
        } for v in rep.claims],
    }


def run_verify_job(data: bytes, filename: str, events: queue.Queue, holder: dict) -> None:
    """线程目标：跑真实流水线，把**真实发生的每一步**推进 events 队列。

    events 上放的都是 JSON 可序列化的 dict；Report 对象与画像走 holder 传递
    （它们不能进 JSON，但 /api/plan 与对话材料需要对象本身）。
    队列最后会放入 None 作为结束哨兵。
    """
    from app.parse import parse_document             # noqa: PLC0415
    from app.pipeline import run_pipeline            # noqa: PLC0415
    from app.profile import derive_profile           # noqa: PLC0415
    from app.llm import build_llm                    # noqa: PLC0415

    def say(*a, **k):
        events.put({"log": " ".join(str(x) for x in a)})

    suffix = Path(filename).suffix or ".pdf"
    tmp = Path(tempfile.NamedTemporaryFile(suffix=suffix, delete=False).name)
    tmp.write_bytes(data)
    started = time.monotonic()
    try:
        # 画像派生是一次较慢的模型调用，前后各发一条日志让用户知道在等什么
        events.put({"log": "归纳候选人画像（身份/行业/层级）…"})
        llm = build_llm()
        profile = derive_profile(parse_document(str(tmp)).safe_text, llm)
        events.put({"log": f"画像完成：identity={profile.identity} level={profile.level} "
                           f"industries={profile.industries or ['—']}"})
        events.put({"stage": "profile"})
        rep = run_pipeline(str(tmp), candidate_name="", profile=profile, progress=say)
        pres = to_presentation(rep)
        holder["oby"] = rep
        holder["profile"] = profile
        events.put({"done": True, "report": pres,
                    "seconds": round(time.monotonic() - started, 1),
                    "filename": filename})
    except Exception as exc:
        events.put({"done": True, "failed": True,
                    "error": f"{type(exc).__name__}: {exc}",
                    "seconds": round(time.monotonic() - started, 1)})
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
        events.put(None)


# ── 对话：直连 DeepSeek 流式（SSE）───────────────────────────────────────
# 之前经 dsh --profile headless 转发，但那是命令行调用，**天生无法流式**——
# 回答要等全部生成完才一次性吐出来。改成同一模型、同一份材料、同一提示词，
# 直接走 DeepSeek 的 stream=true，把增量原样转发给浏览器。
# dsh 保留给多步编排（需要工具调用的场景）；网页对话不需要工具，不需要经它。

_DEEPSEEK_ENDPOINT = "https://api.deepseek.com/chat/completions"


def stream_answer(material: str, question: str):
    """逐段 yield 模型增量。异常以 {"error": ...} 形式 yield，由调用方写成 SSE。"""
    import httpx                                      # noqa: PLC0415
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        yield {"error": "模型没配：.env 里缺 DEEPSEEK_API_KEY"}
        return
    prompt = build_prompt(material, question)
    body = {
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0,
        "stream": True,
        "thinking": {"type": "disabled"},
    }
    try:
        with httpx.stream("POST", _DEEPSEEK_ENDPOINT, json=body,
                          headers={"Authorization": f"Bearer {key}",
                                   "Content-Type": "application/json"},
                          timeout=180.0) as r:
            if r.status_code != 200:
                detail = r.read().decode("utf-8", "ignore")[:200]
                yield {"error": f"模型返回 {r.status_code}：{detail}"}
                return
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    chunk = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = ((chunk.get("choices") or [{}])[0].get("delta") or {}).get("content")
                if delta:
                    yield {"delta": delta}
    except httpx.HTTPError as exc:
        yield {"error": f"模型连接失败：{exc}"}


def report_to_markdown(rep: dict) -> str:
    """把网页端形状的报告转成 Markdown，供导出。**只汇总，不给分不排名。**"""
    L = [f"# 履历核验报告 {rep.get('reportNo', '')}",
         f"候选人：{rep.get('candidate', '')}　应聘：{rep.get('position', '')}",
         f"生成时间：{rep.get('generatedAt', '')}",
         "",
         f"> {rep.get('disclaimer', '')}",
         ""]
    for c in rep.get("claims", []):
        st = STATUS_CN.get(c.get("status"), c.get("status"))
        L.append(f"## {c['id']}　{st}　{c.get('category', '')}"
                 f"{('　来源等级 ' + c['tier']) if c.get('tier') else ''}")
        L.append(f"- 陈述原文：{c.get('rawText', '')}")
        if c.get("proved"):
            L.append(f"- 已证明：{'；'.join(c['proved'])}")
        if c.get("unproved"):
            L.append(f"- 尚未证明：{'；'.join(c['unproved'])}")
        for e in c.get("evidence", []):
            L.append(f"- 证据（{e.get('tier') or '未定级'}）：{e.get('title', '')}"
                     f"｜{e.get('publisher', '')}｜{e.get('url', '')}")
            if e.get("snippet"):
                L.append(f"  - 摘录：{e['snippet'][:200]}")
        if c.get("nextStep"):
            L.append(f"- 下一步：{c['nextStep']}")
        if c.get("question"):
            L.append(f"- 可问的问题：{c['question']}")
        L.append("")
    if rep.get("risks"):
        L.append("## 输入风险")
        for k in rep["risks"]:
            L.append(f"- 位置 {k.get('locator', '')}　类型 {k.get('kind', '')}　{k.get('detail', '')}")
        L.append("")
    L.append("---")
    L.append("本报告仅汇总公开来源与候选人提交的材料，不构成录用决定的唯一依据；"
             "候选人可对任一结论提出异议并补充材料。")
    return "\n".join(L)


STATUS_CN = {"ok": "已证实", "part": "部分证实", "ask": "待澄清",
             "none": "未找到公开记录", "who": "身份未确认"}


# ── HTTP ─────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "underbox"

    # 最近一次**真实核验**的结果。/api/verify 写入，其余端点读取。
    # 页面本身不预置任何数据——没有核验就没有报告。
    oby_report: object | None = None        # open_box 的 Report 对象（对话材料、检索计划用）
    last_presentation: dict | None = None   # 网页端形状（导出用）
    resume_profile: object | None = None    # 画像（检索计划必须用同一份，重新派生会漂移）

    def log_message(self, fmt, *args):        # 安静一点，只留自己的日志
        pass

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, status: int = 200) -> None:
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", status)

    def _asset(self, name: str) -> None:
        p = (HERE / name).resolve()
        if HERE not in p.parents or not p.is_file():
            return self._send(b"not found", "text/plain; charset=utf-8", 404)
        ctype = {"html": "text/html; charset=utf-8", "json": "application/json; charset=utf-8",
                 "css": "text/css; charset=utf-8", "js": "application/javascript; charset=utf-8",
                 # .pdf 要给对类型：查看器的 iframe 降级路径靠它才能内嵌渲染而不是下载
                 "pdf": "application/pdf",
                 }.get(p.suffix.lstrip("."), "application/octet-stream")
        self._send(p.read_bytes(), ctype)

    def _verify_stream(self) -> None:
        """流式核验（SSE）：每条真实日志/阶段变化都推给页面。

        前端的状态窗口就是吃这个端点——所以窗口里的每一行都是流水线里真实发生的，
        不是前端自己编的动画。
        """
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0) or 0)
        if not data:
            return self._json({"error": "没有收到文件"}, 400)
        fname = unquote(self.headers.get("X-Filename") or "resume.pdf").strip()
        fname = Path(fname).name

        events: queue.Queue = queue.Queue()
        holder: dict = {}
        threading.Thread(target=run_verify_job, args=(data, fname, events, holder),
                         daemon=True).start()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # SSE 没有 Content-Length，也不做 chunked 编码——客户端只能靠连接关闭判断结束
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        def push(ev: dict) -> None:
            self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            while True:
                try:
                    ev = events.get(timeout=300)     # 阶段内可能长时间无新行，放宽到 5 分钟
                except queue.Empty:
                    push({"ping": True})             # 有心跳，浏览器就知道连接还活着
                    continue
                if ev is None:
                    break
                push(ev)
                if ev.get("done"):
                    if "report" in ev:               # 存到**类**上（实例随请求销毁）
                        Handler.last_presentation = ev["report"]
                        Handler.oby_report = holder.get("oby")
                        Handler.resume_profile = holder.get("profile")
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass                                     # 用户关了页面 / 中断了连接

    def do_GET(self):                          # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._asset("index.html")
        if path == "/api/health":
            return self._json({
                "ok": True,
                "verify": _pipeline_ready(),
                "chat": True,
                "llmConfigured": _llm_ready(),
                "searchConfigured": _search_configured(),
                "hasReport": self.last_presentation is not None,
                "model": "deepseek-flash",
            })
        if path == "/api/report":
            # 页面刷新后把最近一次真实报告交还——不然用户一刷新就"丢"了报告
            if self.last_presentation is None:
                return self._json({"error": "还没有核验任何简历"}, 404)
            return self._json({"report": self.last_presentation})
        if path == "/api/export":
            fmt = (self.path.split("?", 1)[1] if "?" in self.path else "").lower()
            return self.do_GET_export("md" if "md" in fmt else "json")
        return self._asset(path.lstrip("/"))

    def do_POST(self):                         # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/verify/stream":
            return self._verify_stream()
        if path == "/api/ask/stream":
            return self._ask_stream()
        if path == "/api/plan":
            return self._plan()
        return self._json({"error": "not found"}, 404)

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def _ask_stream(self) -> None:
        """流式问答（SSE）。材料来自**最近一次真实核验**的报告，没有任何预置数据。"""
        payload = self._read_json()
        question = (payload.get("question") or "").strip()
        if not question:
            return self._json({"error": "问题不能为空"}, 400)
        if len(question) > 2000:
            return self._json({"error": "问题太长了（上限 2000 字）"}, 400)
        if self.oby_report is None:
            return self._json({"error": "还没有核验任何简历——先上传一份，问答才有材料"}, 409)

        # 材料整理必须放在**发响应头之前**并兜住异常：一旦头发出去了再崩，
        # 浏览器只会看到无声断连（"Failed to fetch"），永远不知道服务端发生了什么。
        try:
            material = render_material(self.oby_report)
        except Exception as exc:
            return self._json({"error": f"整理对话材料失败：{type(exc).__name__}: {exc}"}, 500)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # 不用 keep-alive：SSE 没有 Content-Length，也不做 chunked 编码，
        # 客户端只能靠**连接关闭**判断流结束。保持连接 = 客户端永远等不到收尾。
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        started = time.monotonic()
        acc: list[str] = []
        try:
            for ev in stream_answer(material, question):
                if "error" in ev:
                    self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    return
                acc.append(ev["delta"])
                self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(f"data: {json.dumps({'done': True, 'seconds': round(time.monotonic() - started, 1)}, ensure_ascii=False)}\n\n".encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 客户端中断（用户点了停止 / 关了页面）——正常收尾，不算错误
            pass

    def _plan(self) -> None:
        """某条陈述的检索计划：策略引擎实际会发哪些查询、为什么。"""
        from app.plan import plan_with_notes               # noqa: PLC0415
        payload = self._read_json()
        cid = payload.get("claimId") or ""
        rep = self.oby_report
        if rep is None:
            return self._json({"error": "还没有核验任何简历"}, 409)
        vc = next((v for v in rep.claims if v.claim.id == cid), None)
        if vc is None:
            return self._json({"error": f"报告里没有 {cid}"}, 404)
        cats = [v.claim.category for v in rep.claims]
        plan = plan_with_notes(vc.claim, rep.candidate_name, self.resume_profile, cats)
        return self._json({
            "claimId": cid,
            "strategy": {"id": plan.profile_id, "name": plan.profile_name},
            "budget": plan.budget,
            "queries": [{"text": q.text, "site": q.site, "kind": q.kind,
                         "element": q.element, "round": q.round,
                         "weight": q.weight, "purpose": q.purpose} for q in plan.queries],
            "droppedByBudget": plan.dropped,
            "notes": plan.notes,
        })

    def do_GET_export(self, fmt: str) -> None:      # noqa: N802
        rep = self.last_presentation
        if rep is None:
            return self._json({"error": "还没有核验任何简历"}, 409)
        no = re.sub(r"[^\w.-]", "", rep.get("reportNo") or "report") or "report"
        if fmt == "md":
            body = report_to_markdown(rep).encode("utf-8")
            ctype, name = "text/markdown; charset=utf-8", f"{no}.md"
        else:
            body = json.dumps(rep, ensure_ascii=False, indent=2).encode("utf-8")
            ctype, name = "application/json; charset=utf-8", f"{no}.json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    ap = argparse.ArgumentParser(description="underbox 本地服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()

    print(f"underbox  http://{args.host}:{args.port}/", flush=True)
    print(f"  模型     {'deepseek-flash' if _llm_ready() else '⚠ 未配置 DEEPSEEK_API_KEY'}", flush=True)
    print(f"  检索     {'智谱 web_search' if _search_configured() else '⚠ 未配置 SEARCH_API_KEY（检索为空，结果会如实标 none）'}", flush=True)
    print("  数据     无预置报告——上传简历后由真实流水线产生", flush=True)
    print("  Ctrl-C 停止", flush=True)

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
