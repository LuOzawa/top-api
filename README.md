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
| `GET /api/top` | 榜单 JSON，参数：`slot`(morning/evening)、`limit`(1-30)、`platform` |
| `GET /api/trend` | **跨平台趋势**：`cross_platform`(同内容多平台上榜)、`rising`(连续多日上升)、`top_domains`；参数 `limit` |
| `GET /api/db` | **数据库连通性**：PG 版本 / 库名 / 用户 / 服务端时间 / 连接耗时 |
| `GET /docs` | Swagger 自动文档 |

示例：

```bash
curl -s "https://top-api-326624733889.us-central1.run.app/api/top?limit=3"
curl -s ".../api/top?platform=hn&limit=5"
curl -s ".../api/trend?limit=10"
```

## 数据管道与 BigQuery

- 每天 4 次抓取后，VM 上的 `export_digest.py` 自动执行：
  1. 生成 `digest/latest.json`（日报 + 趋势）上传 GCS，API 60 秒内自动生效
  2. 当天明细 NDJSON 上传 GCS，并提交 **BigQuery 增量加载**（按 `day` 分区，`WRITE_TRUNCATE` 只替换当天分区）
- BigQuery 数据集：`crawler.dg_items`（day / slot / platform / item_id / title / url / points / comments）

## Cloud SQL Postgres

实例：`digest-pg`（PostgreSQL 16 / db-f1-micro / us-central1 / 10GB SSD / 每日 03:30 自动备份）

**连接方式：Unix socket，不走公网**

```
Cloud Run  ──/cloudsql/<PROJECT>:us-central1:digest-pg──►  Cloud SQL
```

- Cloud Run 挂载：`--add-cloudsql-instances`
- 密码：**Secret Manager**（`db-password`）→ 运行时注入 `DB_PASSWORD`，不落代码/镜像
- 需要的 IAM：Cloud Run 服务账号需 `roles/cloudsql.client` + `roles/secretmanager.secretAccessor`

**当前性能基线（实测）**：

| 场景 | 耗时 |
|---|---|
| 冷启动首次连接 | 375 ms |
| 每请求新建连接 | 27–29 ms |

> 每次请求新建连接，在并发下会打满 `max_connections` → 下一步用连接池（psycopg_pool）把耗时压到个位数 ms。

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
