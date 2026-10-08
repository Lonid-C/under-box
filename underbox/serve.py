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
import hashlib
import hmac
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


# ── 登录门禁 ──────────────────────────────────────────────────────────────
# 口令来自环境变量（部署方写进 open_box/.env 的 UNDERBOX_PASSWORD，app/env.py 会加载）。
# **没设口令 = 门禁关闭**，服务就是开放的——本地开发不必每次登录。
COOKIE_NAME = "ub_gate"


def _gate_password() -> str:
    return (os.environ.get("UNDERBOX_PASSWORD") or "").strip()


def _session_token(password: str) -> str:
    """由口令派生的会话值（cookie 里存这个，不存口令本身）。

    服务端不保存任何会话状态：重启不掉线，也没有过期表要清理。
    换来的是"改口令即让所有旧 cookie 失效"——对一个本地小服务，这笔划算。
    """
    return hmac.new(password.encode("utf-8"), b"underbox-session-v1", hashlib.sha256).hexdigest()


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
        for note in getattr(v, "source_notes", []):
            out.append(f"  官网名单范围：{note}")
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
    skipped = getattr(rep, "skipped_claims", None) or []
    if skipped:
        out.append("按设置没有核验的条目（没有检索，报告对它们不下结论）：")
        for c in skipped:
            out.append(f"  [{c.id}] {c.category}　{c.raw_text}")
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
    return bool(os.environ.get("SEARCH_API_KEY") or os.environ.get("BRAVE_SEARCH_API_KEY"))


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
        # 拆出来但按设置不核验的（默认实习经历）：如实列出，导出时写明没核
        "skipped": [claim_brief(c) for c in getattr(rep, "skipped_claims", None) or []],
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
            "sourceNotes": getattr(v, "source_notes", []),
            "competitionLookup": getattr(v, "competition_lookup", None),
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
    from app.llm import build_llm                    # noqa: PLC0415
    from app.resume_gate import ensure_resume         # noqa: PLC0415

    def say(*a, **k):
        events.put({"log": " ".join(str(x) for x in a)})

    suffix = Path(filename).suffix or ".upload"
    tmp = Path(tempfile.NamedTemporaryFile(suffix=suffix, delete=False).name)
    tmp.write_bytes(data)
    started = time.monotonic()
    try:
        parsed = parse_document(tmp)
        events.put({"log": f"识别文件格式：{parsed.kind.upper()}；正在判断是否为简历"})
        llm = build_llm()
        ensure_resume(parsed, llm)
        events.put({"log": "已确认是个人简历，开始搜索准备"})
        # 画像派生是一次较慢的模型调用；现在和陈述拆分同时发出（见 run_pipeline），
        # 画像一出来就通过 on_profile 回调推一条日志，前端据此推进阶段。
        events.put({"log": "归纳候选人画像（身份/行业/层级），同时拆分陈述…"})

        def on_profile(profile):
            holder["profile"] = profile
            events.put({"log": f"画像完成：identity={profile.identity} level={profile.level} "
                               f"industries={profile.industries or ['—']}"})
            events.put({"stage": "profile"})

        rep = run_pipeline(str(tmp), candidate_name="", parsed_document=parsed,
                           llm=llm, progress=say, on_profile=on_profile)
        pres = to_presentation(rep)
        holder["oby"] = rep
        events.put({"done": True, "report": pres,
                    "seconds": round(time.monotonic() - started, 1),
                    "filename": filename, "fileType": parsed.kind})
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


# ── 先拆分、再勾选、再核验 ──────────────────────────────────────────────
# 页面先上传简历 → 服务端解析、画像、拆成陈述（不检索）→ 页面按类别分块展示，
# 用户勾选要核验的条目 → 只检索勾选的那些。拆分结果按 token 暂存在内存里，只保留最近几份。

PREPARED: dict[str, dict] = {}
_PREPARED_LOCK = threading.Lock()
_PREPARED_KEEP = 6


def _remember_prepared(entry: dict) -> str:
    import uuid                                        # noqa: PLC0415
    token = uuid.uuid4().hex
    with _PREPARED_LOCK:
        PREPARED[token] = entry
        while len(PREPARED) > _PREPARED_KEEP:
            PREPARED.pop(next(iter(PREPARED)))
    return token


def claim_brief(c) -> dict:
    return {"id": c.id, "category": c.category, "date": c.date_label, "rawText": c.raw_text,
            "locator": c.raw_locator, "elements": c.elements}


def run_split_job(data: bytes, filename: str, events: queue.Queue, holder: dict) -> None:
    """线程目标：解析 → 确认是简历 → 画像与拆分（同时进行）。结束时推送陈述清单和 token。"""
    from app.parse import parse_document             # noqa: PLC0415
    from app.pipeline import prepare_resume          # noqa: PLC0415
    from app.llm import build_llm                    # noqa: PLC0415
    from app.resume_gate import ensure_resume         # noqa: PLC0415

    def say(*a, **k):
        events.put({"log": " ".join(str(x) for x in a)})

    suffix = Path(filename).suffix or ".upload"
    tmp = Path(tempfile.NamedTemporaryFile(suffix=suffix, delete=False).name)
    tmp.write_bytes(data)
    started = time.monotonic()
    try:
        parsed = parse_document(tmp)
        events.put({"log": f"识别文件格式：{parsed.kind.upper()}；正在判断是否为简历"})
        llm = build_llm()
        ensure_resume(parsed, llm)
        events.put({"log": "已确认是个人简历"})
        events.put({"log": "归纳候选人画像（身份/行业/层级），同时拆分陈述…"})

        def on_profile(profile):
            events.put({"log": f"画像完成：identity={profile.identity} level={profile.level} "
                               f"industries={profile.industries or ['—']}"})
            events.put({"stage": "profile"})

        prepared = prepare_resume(str(tmp), parsed_document=parsed, llm=llm, progress=say,
                                  on_profile=on_profile)
        token = _remember_prepared({"prepared": prepared, "filename": filename,
                                    "fileType": parsed.kind, "at": time.time()})
        events.put({"log": f"已拆成 {len(prepared.claims)} 条陈述，等待勾选"
                           + (f"（另有 {len(prepared.skipped)} 条按设置不核验）" if prepared.skipped else "")})
        events.put({"done": True, "split": {
            "token": token,
            "candidate": prepared.name,
            "fileType": parsed.kind,
            "filename": filename,
            "risks": [{"kind": r.kind, "locator": r.locator, "detail": r.detail, "excerpt": r.excerpt}
                      for r in parsed.risks],
            "claims": [claim_brief(c) for c in prepared.claims],
            "skipped": [claim_brief(c) for c in prepared.skipped],
        }, "seconds": round(time.monotonic() - started, 1)})
    except Exception as exc:
        events.put({"done": True, "failed": True, "error": _user_error(exc),
                    "seconds": round(time.monotonic() - started, 1)})
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
        events.put(None)


def _user_error(exc: Exception) -> str:
    """给页面看的错误：已知的输入问题只说原因，其余带上异常类型方便排查。"""
    try:
        from app.homepage import HomepageError            # noqa: PLC0415
        from app.resume_gate import NotResumeError         # noqa: PLC0415
        known = (HomepageError, NotResumeError)
    except Exception:
        known = ()
    if known and isinstance(exc, known):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def run_split_url_job(url: str, events: queue.Queue, holder: dict) -> None:
    """线程目标：个人主页 → 读取正文 → 确认是个人主页（防开盒）→ 画像与拆分。

    读取守 robots.txt、如实 UA、同站限速，只取正文文字。确认不是个人主页就停，
    不拆分、不检索。之后和上传文件完全同一条流水线。
    """
    from urllib.parse import urlparse                   # noqa: PLC0415
    from app.homepage import ensure_personal_homepage, read_homepage, to_document  # noqa: PLC0415
    from app.pipeline import prepare_resume             # noqa: PLC0415
    from app.llm import build_llm                       # noqa: PLC0415

    def say(*a, **k):
        events.put({"log": " ".join(str(x) for x in a)})

    started = time.monotonic()
    try:
        events.put({"log": f"读取网页：{urlparse(url if '://' in url else 'https://' + url).hostname or url}"
                           "（守 robots.txt，只取正文文字）"})
        snap = read_homepage(url)
        doc = to_document(snap)
        hidden = sum(1 for r in doc.risks if r.kind == "hidden_html")
        events.put({"log": f"已读取正文 {len(snap.blocks)} 段"
                           + (f"；发现 {hidden} 处隐藏文字，已剔除" if hidden else "")})
        llm = build_llm()
        events.put({"log": "判断这是不是个人主页（防止拿来查无关的人）…"})
        verdict = ensure_personal_homepage(doc, llm, title=snap.title)
        events.put({"log": f"已确认：{verdict['page_type']}"
                           + (f"（{verdict['person']}）" if verdict.get("person") else "")
                           + (f"——{verdict['reason']}" if verdict.get("reason") else "")})
        events.put({"log": "归纳画像（身份/行业/层级），同时拆分陈述…"})

        def on_profile(profile):
            events.put({"log": f"画像完成：identity={profile.identity} level={profile.level} "
                               f"industries={profile.industries or ['—']}"})
            events.put({"stage": "profile"})

        prepared = prepare_resume(doc.source, parsed_document=doc, llm=llm, progress=say,
                                  on_profile=on_profile)
        token = _remember_prepared({"prepared": prepared, "filename": snap.site or url,
                                    "fileType": "web", "at": time.time()})
        events.put({"log": f"已拆成 {len(prepared.claims)} 条陈述，等待勾选"
                           + (f"（另有 {len(prepared.skipped)} 条按设置不核验）" if prepared.skipped else "")})
        events.put({"done": True, "split": {
            "token": token,
            "candidate": prepared.name,
            "fileType": "web",
            "filename": snap.title or snap.site,
            "risks": [{"kind": r.kind, "locator": r.locator, "detail": r.detail, "excerpt": r.excerpt}
                      for r in doc.risks],
            "claims": [claim_brief(c) for c in prepared.claims],
            "skipped": [claim_brief(c) for c in prepared.skipped],
            "preview": snap.preview(),
            "verdict": verdict,
        }, "seconds": round(time.monotonic() - started, 1)})
    except Exception as exc:
        events.put({"done": True, "failed": True, "error": _user_error(exc),
                    "seconds": round(time.monotonic() - started, 1)})
    finally:
        events.put(None)


def run_verify_selected_job(token: str, claim_ids: list, events: queue.Queue, holder: dict) -> None:
    """线程目标：只核验勾选的陈述（各条同时开始检索）。"""
    from app.pipeline import verify_prepared         # noqa: PLC0415

    def say(*a, **k):
        events.put({"log": " ".join(str(x) for x in a)})

    started = time.monotonic()
    try:
        with _PREPARED_LOCK:
            entry = PREPARED.get(token)
        if entry is None:
            raise LookupError("这份简历的拆分结果已过期，请重新上传")
        prepared = entry["prepared"]
        rep = verify_prepared(prepared, claim_ids=claim_ids, progress=say)
        holder["oby"] = rep
        holder["profile"] = prepared.profile
        events.put({"done": True, "report": to_presentation(rep),
                    "seconds": round(time.monotonic() - started, 1),
                    "filename": entry["filename"], "fileType": entry["fileType"]})
    except Exception as exc:
        events.put({"done": True, "failed": True,
                    "error": f"{type(exc).__name__}: {exc}",
                    "seconds": round(time.monotonic() - started, 1)})
    finally:
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
        "stream_options": {"include_usage": True},      # 末尾多一块带 usage 的 chunk，用于记账
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
                if chunk.get("usage"):
                    from app.usage import METER            # noqa: PLC0415
                    METER.record_llm("deepseek", chunk["usage"])
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
        for note in c.get("sourceNotes", []):
            L.append(f"- 官网名单范围：{note}")
        competition = c.get("competitionLookup") or {}
        if competition.get("official_urls"):
            L.append(f"- 赛事官网：{'；'.join(competition['official_urls'])}")
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
    if rep.get("skipped"):
        L.append("## 未核验的条目")
        L.append("以下条目按设置不进入核验（实习经历通常没有公开记录），报告对它们不下任何结论。")
        for c in rep["skipped"]:
            L.append(f"- {c.get('category', '')}：{c.get('rawText', '')}")
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

    def _send(self, body: bytes, ctype: str, status: int = 200,
              headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict, status: int = 200, headers: dict | None = None) -> None:
        self._send(json.dumps(data, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8", status, headers)

    # ── 门禁 ──────────────────────────────────────────────────────────────
    def _cookie(self, name: str) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return unquote(v)
        return ""

    def _authed(self) -> bool:
        pw = _gate_password()
        if not pw:
            return True                        # 没设口令：门禁关闭
        return hmac.compare_digest(self._cookie(COOKIE_NAME), _session_token(pw))

    def _deny_api(self, path: str) -> bool:
        """需要登录却被拦下就回 401，返回 True 表示已经处理完这个请求。

        只拦 /api/*；页面本身（index.html 等静态资源）必须放行，
        否则登录页自己都拿不到。拦住 API 就等于拦住了全部数据。
        """
        if not path.startswith("/api/"):
            return False
        if path in ("/api/login", "/api/logout", "/api/session", "/api/health"):
            return False                       # 登录用 / 探活用，必须放行
        if self._authed():
            return False
        self._json({"error": "未登录或登录已过期", "loginRequired": True}, 401)
        return True

    def _login(self) -> None:
        pw = _gate_password()
        given = str(self._read_json().get("password") or "")
        if not pw:                             # 没设口令：门禁关闭，前端据此直接进主页
            return self._json({"ok": True, "gate": False})
        if not given or not hmac.compare_digest(given, pw):
            time.sleep(0.35)                   # 轻微迟滞，压低暴力尝试的速率
            return self._json({"ok": False, "error": "口令不正确"}, 401)
        self._json({"ok": True}, 200, {
            "Set-Cookie": f"{COOKIE_NAME}={_session_token(pw)}; Path=/; "
                          f"Max-Age={7 * 86400}; HttpOnly; SameSite=Lax",
        })

    def _logout(self) -> None:
        self._json({"ok": True}, 200, {
            "Set-Cookie": f"{COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax",
        })

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
        """流式核验（SSE）。两种请求：

        · JSON {"token", "claimIds"}：核验先前 /api/split/stream 拆好的、用户勾选的条目；
        · 文件本体（旧接口）：一次跑完拆分 + 全部条目。
        """
        ctype = (self.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            payload = self._read_json()
            token = str(payload.get("token") or "")
            ids = [str(x) for x in (payload.get("claimIds") or []) if str(x)]
            if not token:
                return self._json({"error": "缺少 token：先上传简历拆分条目"}, 400)
            if not ids:
                return self._json({"error": "没有勾选要核验的条目"}, 400)
            return self._sse(run_verify_selected_job, (token, ids))
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0) or 0)
        if not data:
            return self._json({"error": "没有收到文件"}, 400)
        fname = Path(unquote(self.headers.get("X-Filename") or "resume.pdf").strip()).name
        return self._sse(run_verify_job, (data, fname))

    def _split_stream(self) -> None:
        """流式拆分（SSE）：上传简历 → 解析、画像、拆成陈述，结束时给出陈述清单与 token。不检索。

        JSON {"url": ...} 是个人主页模式：读取网页、确认是个人主页后同样拆分。
        """
        if "application/json" in (self.headers.get("Content-Type") or "").lower():
            url = str(self._read_json().get("url") or "").strip()
            if not url:
                return self._json({"error": "请粘贴个人主页的网址"}, 400)
            return self._sse(run_split_url_job, (url,))
        data = self.rfile.read(int(self.headers.get("Content-Length") or 0) or 0)
        if not data:
            return self._json({"error": "没有收到文件"}, 400)
        fname = Path(unquote(self.headers.get("X-Filename") or "resume.pdf").strip()).name
        return self._sse(run_split_job, (data, fname))

    def _sse(self, target, args: tuple) -> None:
        """把后台任务的事件原样推给页面。前端的状态窗口就是吃这个——每一行都是真实发生的。"""
        from app.usage import METER                       # noqa: PLC0415
        events: queue.Queue = queue.Queue()
        holder: dict = {}
        # 用量：每次供应商调用记账后，把「本任务自开始以来」的累计推给页面
        base = METER.mark()
        unsubscribe = METER.subscribe(lambda: events.put({"usage": METER.since(base)}))
        threading.Thread(target=target, args=(*args, events, holder), daemon=True).start()

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
                if ev.get("done"):
                    ev["usage"] = METER.since(base)
                push(ev)
                if ev.get("done"):
                    if "report" in ev:               # 存到**类**上（实例随请求销毁）
                        Handler.last_presentation = ev["report"]
                        Handler.oby_report = holder.get("oby")
                        Handler.resume_profile = holder.get("profile")
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass                                     # 用户关了页面 / 中断了连接
        finally:
            unsubscribe()

    def do_GET(self):                          # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._asset("index.html")
        if path == "/api/session":
            # 页面靠它决定"显示登录屏还是主界面"
            return self._json({"gate": bool(_gate_password()), "authed": self._authed()})
        if self._deny_api(path):
            return
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
        if path == "/api/login":
            return self._login()
        if path == "/api/logout":
            return self._logout()
        if self._deny_api(path):
            return
        if path == "/api/verify/stream":
            return self._verify_stream()
        if path == "/api/split/stream":
            return self._split_stream()
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
        from app.usage import METER                       # noqa: PLC0415
        base = METER.mark()
        try:
            for ev in stream_answer(material, question):
                if "error" in ev:
                    self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    return
                acc.append(ev["delta"])
                self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(f"data: {json.dumps({'done': True, 'seconds': round(time.monotonic() - started, 1), 'usage': METER.since(base)}, ensure_ascii=False)}\n\n".encode("utf-8"))
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

    accounts_file = os.environ.get("UNDERBOX_ACCOUNTS_FILE")
    if accounts_file:
        from accounts import serve_accounts
        return serve_accounts(Handler, accounts_file, args.host, args.port)

    print(f"underbox  http://{args.host}:{args.port}/", flush=True)
    print(f"  模型     {'deepseek-flash' if _llm_ready() else '⚠ 未配置 DEEPSEEK_API_KEY'}", flush=True)
    provider_label = ("智谱 web_search + Brave 备用" if os.environ.get("SEARCH_API_KEY") and
                      os.environ.get("BRAVE_SEARCH_API_KEY") else
                      "智谱 web_search" if os.environ.get("SEARCH_API_KEY") else
                      "Brave Web Search" if os.environ.get("BRAVE_SEARCH_API_KEY") else
                      "⚠ 未配置搜索 key（检索为空，结果会如实标 none）")
    print(f"  检索     {provider_label}", flush=True)
    print("  数据     无预置报告——上传简历后由真实流水线产生", flush=True)
    print("  Ctrl-C 停止", flush=True)

    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
