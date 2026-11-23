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
| `GET /docs` | Swagger 自动文档 |

示例：

```bash
curl -s "https://top-api-326624733889.us-central1.run.app/api/top?limit=3"
curl -s ".../api/top?platform=hn&limit=5"
```

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
