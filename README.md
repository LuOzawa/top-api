# top-api

技术日报数据 API —— 跑在 **Cloud Run** 上的 FastAPI 服务，数据来自 GCS 上的公开 JSON。

线上地址：<https://top-api-326624733889.us-central1.run.app>

## 架构

```
GCE VM (e2-micro)                Cloud Storage              Cloud Run
┌─────────────────┐        ┌────────────────────┐      ┌──────────────────┐
│ crawler 抓 8 个  │ 每天4次│ digest/latest.json │ 读取 │ top-api (FastAPI)│
│ 平台到 SQLite    │ ─────► │ 公开只读 (36 KB)    │ ───► │ 512Mi / 缩容到 0  │
└─────────────────┘ 导出    └────────────────────┘      └──────────────────┘
                                                              ▲
                                        git push → Cloud Build ┘
```

## 端点

| 路径 | 说明 |
|---|---|
| `GET /` | 榜单 HTML 页 |
| `GET /api/health` | 健康检查 |
| `GET /api/top` | 榜单 JSON。参数：`slot`、`limit`(1-30)、`platform`、`source`(`auto`默认/`db`/`gcs`)。默认查 **Postgres**，失败自动回退 GCS，响应里 `source` 字段标明实际来源 |
| `GET /api/trend` | **跨平台趋势**：`cross_platform`(同内容多平台上榜)、`rising`(连续多日上升)、`top_domains`；参数 `limit` |
| `GET /api/db` | **数据库连通性**：PG 版本 / 库名 / 用户 / 服务端时间 / 连接耗时 |
| `GET /docs` | Swagger 自动文档 |

### 收藏（写路径，需 `X-API-Key`）

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/favorites` | 新增收藏。**幂等**：同一 URL 重复提交返回 200 + 原记录（不产生重复行）；新建返回 201 |
| `GET` | `/api/favorites` | 列表，参数 `limit`(1-100) / `offset` / `platform` |
| `PATCH` | `/api/favorites/{id}` | 改 `title` / `note` |
| `DELETE` | `/api/favorites/{id}` | 删除（204）|
| `GET` | `/api/favorites/stats` | 按平台统计 |

**鉴权**：写操作需要请求头 `X-API-Key`，Key 存在 **Secret Manager**（`api-key`），运行时注入，不进代码/镜像。读接口（top/trend/health）公开。

**错误码约定**：`400` 空更新 · `401` Key 无效/缺失 · `404` id 不存在 · `422` 参数校验失败 · `503` 依赖不可用

示例：

```bash
BASE="https://top-api-326624733889.us-central1.run.app"
curl -s "$BASE/api/top?limit=3"
curl -s "$BASE/api/trend?limit=10"

# 写操作（K 为你的 API Key）
K="<api-key>"
curl -s -X POST "$BASE/api/favorites" -H "X-API-Key: $K" -H 'Content-Type: application/json' \
     -d '{"url":"https://example.com/post","title":"示例","platform":"hn"}'
curl -s "$BASE/api/favorites?limit=10" -H "X-API-Key: $K"
curl -s -X PATCH "$BASE/api/favorites/1" -H "X-API-Key: $K" -H 'Content-Type: application/json' -d '{"note":"稍后读"}'
curl -s -X DELETE "$BASE/api/favorites/1" -H "X-API-Key: $K"
```

## 数据库迁移与测试

```bash
# 迁移（版本化 SQL，记录在 schema_migrations 表，可重复执行）
python run_migrations.py --status    # 看状态
python run_migrations.py             # 应用未执行的迁移

# 测试（需要 Postgres；CI 里用临时容器跑真实 CRUD）
PG_DSN=postgresql://postgres:postgres@localhost:5432/postgres \
API_KEY=test-key OWNER=me pytest -q
```

CI 流程：**test → deploy**（测试不过，部署直接跳过）。

## 数据管道与 BigQuery

每天 4 次抓取后，VM 上的 `export_digest.py` 一次产出三份数据：

| 目标 | 内容 | 用途 |
|---|---|---|
| **GCS** `digest/latest.json` | 日报 + 趋势分析 | API 的 GCS 回退路径（60 秒缓存）|
| **BigQuery** `crawler.dg_items` | 当天明细 NDJSON（按 day 分区，只替换当天）| 历史分析 / 跨平台共振查询 |
| **Cloud SQL** `digest_items` | 当天明细 upsert（幂等）| `/api/top` 的主数据源 |

```
VM crawler → SQLite → export_digest.py ─┬→ GCS latest.json ──┐
                                        ├→ BigQuery (分区) ──┤
                                        └→ Cloud SQL PG ─────┴→ Cloud Run API
```

## Cloud SQL Postgres

实例：`digest-pg`（PostgreSQL 16 / db-f1-micro / us-central1 / 10GB SSD / 每日 03:30 自动备份）

**连接方式：Unix socket，不走公网**

```
Cloud Run  ──/cloudsql/<PROJECT>:us-central1:digest-pg──►  Cloud SQL
```

- Cloud Run 挂载：`--add-cloudsql-instances`
- 密码：**Secret Manager**（`db-password`）→ 运行时注入 `DB_PASSWORD`，不落代码/镜像
- 需要的 IAM：Cloud Run 服务账号需 `roles/cloudsql.client` + `roles/secretmanager.secretAccessor`

**连接池**：`psycopg_pool.ConnectionPool`（min 1 / max `POOL_MAX`=8），实例内复用连接。

**连接层性能（实测）**：

| 场景 | 耗时 |
|---|---|
| 冷启动首次连接 | 375 ms |
| 每请求新建连接（优化前）| 27–29 ms |
| **连接池（现在）** | **3.3–4.8 ms** |

## 监控与告警

Cloud Monitoring 告警策略（通知到邮件）：

| 策略 | 条件 |
|---|---|
| top-api · 5xx 错误率 | `run.googleapis.com/request_count` 5xx 类 > 0.05/s，持续 5 分钟 |
| top-api · P95 延迟 | `request_latencies` P95 > 2s，持续 5 分钟 |
| Cloud SQL · 连接数 | `num_backends` > 20（db-f1-micro 上限约 25）|
| luozawa-site · 拨测 | 站点不可用，持续 1 分钟 |

## 压测结果

从**同区域 GCE 实例**发起（排除跨境网络干扰），500 请求 / 并发 50：

| 路径 | RPS | p50 | p95 | 状态码 |
|---|---|---|---|---|
| `/api/top`（Postgres，**优化前**）| 228 | 215 ms | 266 ms | 100% 200 |
| `/api/top`（Postgres，**优化后**）| **375** | **112 ms** | **162 ms** | 100% 200 |
| `/api/top?source=gcs`（内存缓存）| 356 | 124 ms | 166 ms | 100% 200 |
| `/api/db`（纯查询）| 554 | 52 ms | 120 ms | 100% 200 |

并发 100（1000 请求）：**341 RPS**，p50 199 ms，p95 281 ms，**100% 200**；p99 出现 1.2s 长尾，来自 Cloud Run 冷启动扩容。

**优化动作**：压测发现连接池 `max_size=4` 成为瓶颈（50 并发排队等连接），改为 8 + 给 DB 路径加 30 秒结果缓存 → **RPS +64%，p50 降低 48%**。

## 本地运行

```bash
pip install -r requirements.txt
DATA_BUCKET=luozawa-top-data uvicorn main:app --reload --port 8080
```

## 部署

自动：push 到 `main` → Cloud Build 触发 → 构建镜像 → 部署新 revision。

手动：

```bash
gcloud builds submit --config cloudbuild.yaml .
```

## 设计要点

- **数据与代码解耦**：API 不打包数据，运行时从 GCS 拉取，数据每天更新无需重新部署
- **60 秒内存缓存**：避免每次请求都打 GCS
- **缩容到 0**：无人访问时零成本（免费额度内）
- **非 root 运行**：容器内用 UID 1001，`COPY --chown` 保证可读
