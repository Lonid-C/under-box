"""HTTP 服务：GET / 返回报告页，GET /api/report/{id} 返回报告 JSON。

优先使用 FastAPI；环境里没有 FastAPI 时退到 Starlette（同一套 ASGI 路由），
两者都没有时 `python -m app.main` 还有一个 stdlib http.server 兜底，
保证"演示时不能因为缺依赖跑不起来"。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
FIXTURES = ROOT / "fixtures"
OUT = ROOT / "out"

MODE = os.environ.get("MODE", "mock")


# --------------------------------------------------------------------------
# 数据读取
# --------------------------------------------------------------------------

def report_path(report_id: str) -> Path | None:
    """live 模式跑出来的报告优先；否则回落到内置 fixture。"""
    safe = "".join(ch for ch in report_id if ch.isalnum() or ch in "-_")
    for p in (OUT / f"report_{safe}.json", FIXTURES / f"report_{safe}.json"):
        if p.is_file():
            return p
    return None


def load_report(report_id: str) -> dict | None:
    p = report_path(report_id)
    return json.loads(p.read_text(encoding="utf-8")) if p else None


def list_reports() -> list[str]:
    ids = set()
    for d in (FIXTURES, OUT):
        for p in d.glob("report_*.json"):
            ids.add(p.stem[len("report_"):])
    return sorted(ids)


def _asset(name: str) -> tuple[bytes, str] | None:
    p = (WEB / name).resolve()
    if not p.is_file() or WEB.resolve() not in p.parents:
        return None
    ctype = {"css": "text/css; charset=utf-8", "html": "text/html; charset=utf-8",
             "js": "application/javascript; charset=utf-8"}.get(p.suffix.lstrip("."), "application/octet-stream")
    return p.read_bytes(), ctype


# --------------------------------------------------------------------------
# ASGI 应用
# --------------------------------------------------------------------------

def _build_app():
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.responses import FileResponse, JSONResponse

        api = FastAPI(title="履历核验 Demo", docs_url=None, redoc_url=None)

        @api.get("/", include_in_schema=False)
        def index():
            return FileResponse(WEB / "report.html", media_type="text/html; charset=utf-8")

        @api.get("/report.css", include_in_schema=False)
        def css():
            return FileResponse(WEB / "report.css", media_type="text/css; charset=utf-8")

        @api.get("/api/reports")
        def reports():
            return {"mode": MODE, "ids": list_reports()}

        @api.get("/api/report/{report_id}")
        def report(report_id: str):
            data = load_report(report_id)
            if data is None:
                raise HTTPException(status_code=404, detail="report not found")
            return JSONResponse(data)

        return api
    except ImportError:
        pass

    try:
        from starlette.applications import Starlette
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Route
    except ImportError:
        return None

    async def index(request):
        got = _asset("report.html")
        return Response(got[0], media_type=got[1])

    async def css(request):
        got = _asset("report.css")
        return Response(got[0], media_type=got[1])

    async def reports(request):
        return JSONResponse({"mode": MODE, "ids": list_reports()})

    async def report(request):
        data = load_report(request.path_params["report_id"])
        if data is None:
            return JSONResponse({"detail": "report not found"}, status_code=404)
        return JSONResponse(data)

    return Starlette(routes=[
        Route("/", index),
        Route("/report.css", css),
        Route("/api/reports", reports),
        Route("/api/report/{report_id}", report),
    ])


app = _build_app()


# --------------------------------------------------------------------------
# 兜底：stdlib http.server（没有 uvicorn 时用）
# --------------------------------------------------------------------------

def serve_stdlib(host: str = "127.0.0.1", port: int = 8000) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, body: bytes, ctype: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/":
                got = _asset("report.html")
                return self._send(got[0], got[1])
            if path == "/report.css":
                got = _asset("report.css")
                return self._send(got[0], got[1])
            if path == "/api/reports":
                body = json.dumps({"mode": MODE, "ids": list_reports()}, ensure_ascii=False)
                return self._send(body.encode(), "application/json; charset=utf-8")
            if path.startswith("/api/report/"):
                data = load_report(path.rsplit("/", 1)[-1])
                if data is None:
                    return self._send(b'{"detail":"report not found"}', "application/json; charset=utf-8", 404)
                body = json.dumps(data, ensure_ascii=False).encode()
                return self._send(body, "application/json; charset=utf-8")
            self._send(b"not found", "text/plain; charset=utf-8", 404)

        def log_message(self, fmt, *args):
            pass

    print(f"履历核验 Demo（{MODE} 模式）  http://{host}:{port}/")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def main() -> None:
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    if app is None:
        return serve_stdlib(host, port)
    try:
        import uvicorn
    except ImportError:
        return serve_stdlib(host, port)
    print(f"履历核验 Demo（{MODE} 模式）  http://{host}:{port}/")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
