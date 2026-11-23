"""
top-api —— 技术日报数据 API（Cloud Run）

数据来源：GCS 上的公开 JSON（由 VM 上的爬虫每天导出）
  https://storage.googleapis.com/<BUCKET>/digest/latest.json

端点
  GET /                 简单 HTML 页面（演示用）
  GET /api/health       健康检查
  GET /api/top          返回榜单 JSON
      ?slot=morning|evening   默认 morning
      ?limit=1..30            每平台条数，默认 3
      ?platform=hn            只看某个平台
"""
import os
import time

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

BUCKET = os.environ.get("DATA_BUCKET", "luozawa-top-data")
DATA_URL = f"https://storage.googleapis.com/{BUCKET}/digest/latest.json"
CACHE_TTL = int(os.environ.get("CACHE_TTL", "60"))

app = FastAPI(title="luozawa top-api", version="1.0.0")
_cache = {"at": 0.0, "data": None}


def load_data():
    now = time.time()
    if _cache["data"] is not None and now - _cache["at"] < CACHE_TTL:
        return _cache["data"]
    r = httpx.get(DATA_URL, timeout=10)
    r.raise_for_status()
    data = r.json()
    _cache.update(at=now, data=data)
    return data


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@app.get("/api/health")
def health():
    return {"ok": True, "service": "top-api", "data_url": DATA_URL}


@app.get("/api/top")
def top(
    slot: str = Query("morning", pattern="^(morning|evening)$"),
    limit: int = Query(3, ge=1, le=30),
    platform: str | None = None,
):
    try:
        data = load_data()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"data unavailable: {type(e).__name__}")

    slots = data.get("slots", {})
    node = slots.get(slot)
    if not node:
        return {"day": data.get("day"), "slot": slot, "platforms": {}, "note": "该班次暂无数据"}

    plats = node.get("platforms", {})
    if platform:
        plats = {k: v for k, v in plats.items() if k == platform}

    out = {}
    for key, val in plats.items():
        items = val.get("items", [])[:limit]
        out[key] = {"name": val.get("name", key), "count": len(val.get("items", [])), "items": items}

    return {
        "day": node.get("day"),
        "slot": slot,
        "sent_at": node.get("sent_at"),
        "generated_at": data.get("generated_at"),
        "platforms": out,
    }


@app.get("/", response_class=HTMLResponse)
def index():
    try:
        data = load_data()
    except Exception as e:
        return HTMLResponse(
            f"<h1>top-api</h1><p>数据暂不可用：{esc(type(e).__name__)}</p>", status_code=503
        )
    rows = []
    for slot in ("morning", "evening"):
        node = data.get("slots", {}).get(slot)
        if not node:
            continue
        for key, val in node.get("platforms", {}).items():
            top3 = "".join(
                f"<li>{esc(it.get('title', ''))[:110]}"
                f"<span class=muted> · {it.get('points', 0)}</span></li>"
                for it in val.get("items", [])[:3]
            )
            rows.append(
                f"<section><h2>{esc(val.get('name', key))} "
                f"<span class=muted>{slot} · {len(val.get('items', []))}&nbsp;条</span></h2>"
                f"<ol>{top3}</ol></section>"
            )
    html = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>技术日报 · top-api</title>
<style>
 body{{margin:0;background:#0b0f0e;color:#e6ece9;font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif}}
 main{{max-width:860px;margin:0 auto;padding:40px 20px 80px}}
 h1{{font-size:22px;margin:0 0 4px}} .muted{{color:#88958f;font-weight:400;font-size:13px}}
 h2{{font-size:15px;margin:26px 0 6px;color:#7ee2b8}}
 ol{{margin:0;padding-left:20px}} li{{margin:3px 0}}
 footer{{margin-top:36px;color:#66726c;font-size:12px}}
 a{{color:#7ee2b8}}
</style></head><body><main>
<h1>技术日报 <span class=muted>· Cloud Run / FastAPI</span></h1>
<p class=muted>数据日：{esc(str(data.get('day', '?')))} · 生成于 {esc(str(data.get('generated_at', '?')))}</p>
{''.join(rows)}
<footer>API：<a href="/api/top">/api/top</a> ·
 <a href="/api/top?slot=morning&limit=5">?limit=5</a> ·
 <a href="/api/top?platform=hn">?platform=hn</a> ·
 <a href="/docs">Swagger</a></footer>
</main></body></html>"""
    return HTMLResponse(html)
