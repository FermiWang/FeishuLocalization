"""Keep the former HTTP entry point as a data-free HTTPS redirect."""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def redirect(request: Request, path: str):
    if request.method not in ("GET", "HEAD"):
        return JSONResponse({"detail": "请使用 HTTPS 访问会议服务"}, status_code=403)
    target = f"https://192.168.100.179:8766/{path}"
    if request.url.query:
        target += f"?{request.url.query}"
    return RedirectResponse(target, status_code=302)
