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

  —— 收藏（W3 写路径，写操作需 X-API-Key）——
  POST   /api/favorites        新增收藏（幂等：同一 URL 只存一条）
  GET    /api/favorites        列表（分页 + 平台过滤）
  PATCH  /api/favorites/{id}   修改标题/备注
  DELETE /api/favorites/{id}   删除
  GET    /api/favorites/stats  按平台统计
"""
import os
import time

import httpx
import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

BUCKET = os.environ.get("DATA_BUCKET", "luozawa-top-data")
DATA_URL = f"https://storage.googleapis.com/{BUCKET}/digest/latest.json"
CACHE_TTL = int(os.environ.get("CACHE_TTL", "60"))

# ---- Cloud SQL（通过 /cloudsql/<connectionName> 的 Unix socket 连接，
#      不走公网、不需要配置 IP 白名单）----
INSTANCE_CONNECTION_NAME = os.environ.get("INSTANCE_CONNECTION_NAME", "")
DB_USER = os.environ.get("DB_USER", "app")
DB_NAME = os.environ.get("DB_NAME", "digest")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")  # 由 Secret Manager 注入
API_KEY = os.environ.get("API_KEY", "")          # 写操作鉴权（Secret Manager 注入）
OWNER = os.environ.get("OWNER", "me")


# ---------------- 鉴权 ----------------
def require_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    """写操作需要 API Key；Key 只在 Secret Manager 里，不进代码"""
    if not API_KEY:
        raise HTTPException(status_code=503, detail="服务端未配置 API_KEY")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="X-API-Key 无效或缺失")
    return True


# ---------------- 输入模型 ----------------
class FavoriteIn(BaseModel):
    url: str = Field(..., min_length=8, max_length=2000)
    title: str | None = Field(None, max_length=300)
    platform: str | None = Field(None, max_length=40)
    note: str | None = Field(None, max_length=1000)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("url 必须以 http:// 或 https:// 开头")
        return v


class FavoritePatch(BaseModel):
    title: str | None = Field(None, max_length=300)
    note: str | None = Field(None, max_length=1000)


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


# ---------------- 收藏（写路径）----------------
FAV_COLS = "id, url, title, platform, note, created_at, updated_at"


def _fav(row):
    return {
        "id": row[0], "url": row[1], "title": row[2], "platform": row[3], "note": row[4],
        "created_at": row[5].isoformat() if row[5] else None,
        "updated_at": row[6].isoformat() if row[6] else None,
    }


@app.get("/api/favorites/stats")
def favorites_stats(_=Depends(require_key)):
    """按平台统计收藏（必须声明在 /{fav_id} 之前，否则 stats 会被当成 id）"""
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), MIN(created_at), MAX(created_at)"
                    " FROM favorites WHERE owner = %s", (OWNER,))
        total, first, last = cur.fetchone()
        cur.execute("SELECT COALESCE(platform, '(unknown)'), COUNT(*) FROM favorites"
                    " WHERE owner = %s GROUP BY 1 ORDER BY 2 DESC", (OWNER,))
        by_platform = [{"platform": p, "count": n} for p, n in cur.fetchall()]
    return {
        "total": total,
        "first_added": first.isoformat() if first else None,
        "last_added": last.isoformat() if last else None,
        "by_platform": by_platform,
    }


@app.post("/api/favorites", status_code=201)
def add_favorite(item: FavoriteIn, response: Response, _=Depends(require_key)):
    """新增收藏。同一 URL 重复提交是幂等的：返回已有记录（200），不会产生重复行。"""
    sql = f"""
        INSERT INTO favorites (owner, url, title, platform, note)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (owner, url) DO UPDATE SET
          title    = COALESCE(EXCLUDED.title, favorites.title),
          platform = COALESCE(EXCLUDED.platform, favorites.platform),
          note     = COALESCE(EXCLUDED.note, favorites.note),
          updated_at = now()
        RETURNING {FAV_COLS}, (xmax = 0) AS inserted
    """
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (OWNER, item.url, item.title, item.platform, item.note))
        row = cur.fetchone()
    created = bool(row[7])
    response.status_code = 201 if created else 200
    return {"created": created, **_fav(row)}


@app.get("/api/favorites")
def list_favorites(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    platform: str | None = Query(None, max_length=40),
    _=Depends(require_key),
):
    where = "WHERE owner = %s"
    params = [OWNER]
    if platform:
        where += " AND platform = %s"
        params.append(platform)
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM favorites {where}", params)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT {FAV_COLS} FROM favorites {where}"
            " ORDER BY created_at DESC, id DESC LIMIT %s OFFSET %s",
            params + [limit, offset])
        rows = cur.fetchall()
    return {"total": total, "limit": limit, "offset": offset,
            "items": [_fav(r) for r in rows]}


@app.patch("/api/favorites/{fav_id}")
def patch_favorite(fav_id: int, body: FavoritePatch, _=Depends(require_key)):
    sets, params = [], []
    if body.title is not None:
        sets.append("title = %s")
        params.append(body.title)
    if body.note is not None:
        sets.append("note = %s")
        params.append(body.note)
    if not sets:
        raise HTTPException(status_code=400, detail="没有需要更新的字段（title/note 至少给一个）")
    params += [fav_id, OWNER]
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute(f"UPDATE favorites SET {', '.join(sets)}, updated_at = now()"
                    f" WHERE id = %s AND owner = %s RETURNING {FAV_COLS}", params)
        row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"收藏 {fav_id} 不存在")
    return _fav(row)


@app.delete("/api/favorites/{fav_id}", status_code=204)
def delete_favorite(fav_id: int, _=Depends(require_key)):
    with db_connect() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM favorites WHERE id = %s AND owner = %s", (fav_id, OWNER))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"收藏 {fav_id} 不存在")
    return Response(status_code=204)


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
