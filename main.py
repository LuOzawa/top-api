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
  GET /api/trend        跨平台趋势（同内容多平台上榜 / 连续上升 / 域名分布）
  GET /api/db           数据库连通性（Cloud SQL Postgres，走 Unix socket）
"""
import os
import time

import httpx
import psycopg
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

BUCKET = os.environ.get("DATA_BUCKET", "luozawa-top-data")
DATA_URL = f"https://storage.googleapis.com/{BUCKET}/digest/latest.json"
CACHE_TTL = int(os.environ.get("CACHE_TTL", "60"))

# ---- Cloud SQL（通过 /cloudsql/<connectionName> 的 Unix socket 连接，
#      不走公网、不需要配置 IP 白名单）----
INSTANCE_CONNECTION_NAME = os.environ.get("INSTANCE_CONNECTION_NAME", "")
DB_USER = os.environ.get("DB_USER", "app")
DB_NAME = os.environ.get("DB_NAME", "digest")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")  # 由 Secret Manager 注入


# 连接池：Cloud Run 一个实例内复用连接，避免每请求新建
# （每请求新建 ≈ 27ms，且在并发下会打满 db-f1-micro 的 max_connections）
_pool = None


def get_pool():
    global _pool
    if not INSTANCE_CONNECTION_NAME:
        return None
    if _pool is None:
        from psycopg_pool import ConnectionPool
        _pool = ConnectionPool(
            conninfo=(f"host=/cloudsql/{INSTANCE_CONNECTION_NAME} dbname={DB_NAME} "
                      f"user={DB_USER} password={DB_PASSWORD} connect_timeout=5"),
            min_size=1, max_size=4, timeout=5, open=True,
            kwargs={"autocommit": True},
        )
    return _pool


def db_connect():
    pool = get_pool()
    if pool is None:
        raise RuntimeError("INSTANCE_CONNECTION_NAME 未设置")
    return pool.connection()


def db_check():
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT version(), current_database(), current_user, now()")
        return cur.fetchone()

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


PLATFORM_NAMES = {
    "hn": "Hacker News", "lobsters": "Lobsters", "github": "GitHub 热门项目",
    "huggingface": "HuggingFace", "devto": "Dev.to", "arxiv": "arXiv (AI)",
    "stackoverflow": "Stack Overflow", "v2ex": "V2EX",
}


def top_from_db(slot: str, limit: int, platform: str | None):
    """从 Postgres 读榜单（源数据由 VM 每日同步）"""
    sql = ("SELECT platform, title, url, points, comments FROM digest_items"
           " WHERE day = (SELECT MAX(day) FROM digest_items) AND slot = %s")
    params = [slot]
    if platform:
        sql += " AND platform = %s"
        params.append(platform)
    sql += " ORDER BY platform, points DESC"

    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT MAX(day)::text FROM digest_items")
        row = cur.fetchone()
        day = row[0] if row else None
        if not day:
            return None
        cur.execute(sql, params)
        rows = cur.fetchall()

    grouped: dict[str, list] = {}
    for plat, title, url, pts, cmts in rows:
        grouped.setdefault(plat, []).append({
            "title": title, "url": url,
            "points": pts or 0, "comments": cmts or 0,
        })

    platforms = {
        k: {"name": PLATFORM_NAMES.get(k, k), "count": len(v),
            "items": [dict(it, rank=i) for i, it in enumerate(v[:limit], 1)]}
        for k, v in grouped.items()
    }
    return {"day": day, "slot": slot, "platforms": platforms, "source": "postgres"}


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


@app.get("/api/health")
def health():
    return {
        "ok": True,
        "service": "top-api",
        "revision": os.environ.get("K_REVISION", "local"),  # Cloud Run 注入，用于确认版本
        "data_url": DATA_URL,
    }


@app.get("/api/top")
def top(
    slot: str = Query("morning", pattern="^(morning|evening)$"),
    limit: int = Query(3, ge=1, le=30),
    platform: str | None = None,
    source: str = Query("auto", pattern="^(auto|db|gcs)$"),
):
    """榜单数据。source=auto 时优先查 Postgres，失败自动回退到 GCS 快照。"""
    if source in ("auto", "db"):
        try:
            db_result = top_from_db(slot, limit, platform)
            if db_result and db_result.get("platforms"):
                return db_result
        except Exception as e:
            if source == "db":
                raise HTTPException(status_code=503, detail=f"db unavailable: {type(e).__name__}: {str(e)[:100]}")
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
        "source": "gcs",
        "platforms": out,
    }


@app.get("/api/db")
def db_health():
    """验证 Cloud Run → Cloud SQL(Postgres) 的连通性"""
    started = time.time()
    try:
        ver, dbname, user, now = db_check()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"db unavailable: {type(e).__name__}: {str(e)[:120]}")
    return {
        "ok": True,
        "database": dbname,
        "user": user,
        "server_time": str(now),
        "server_version": ver.split(" on ")[0],
        "latency_ms": round((time.time() - started) * 1000, 1),
        "conn": f"/cloudsql/{INSTANCE_CONNECTION_NAME}",
        "pool": _pool_stats(),
    }


def _pool_stats():
    pool = get_pool()
    if pool is None:
        return None
    st = pool.get_stats()
    return {"size": st.get("pool_size"), "available": st.get("pool_available"),
            "requests_queued": st.get("requests_queued")}


@app.get("/api/trend")
def trend(limit: int = Query(10, ge=1, le=50)):
    """跨平台趋势：同一内容出现在多个平台、连续多日上升、上榜域名分布。"""
    try:
        data = load_data()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"data unavailable: {type(e).__name__}")

    tr = data.get("trend") or {}
    if not tr:
        return {"day": data.get("day"), "note": "暂无趋势数据（等待下一次抓取导出）"}
    return {
        "day": data.get("day"),
        "window_days": tr.get("window_days"),
        "generated_at": tr.get("generated_at"),
        "total_items": tr.get("total_items"),
        "cross_platform": tr.get("cross_platform", [])[:limit],
        "rising": tr.get("rising", [])[:limit],
        "top_domains": tr.get("top_domains", [])[:limit],
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
 <a href="/api/trend">/api/trend</a> ·
 <a href="/api/top?slot=morning&limit=5">?limit=5</a> ·
 <a href="/api/top?platform=hn">?platform=hn</a> ·
 <a href="/docs">Swagger</a></footer>
</main></body></html>"""
    return HTMLResponse(html)
