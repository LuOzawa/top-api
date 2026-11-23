# Cloud SQL 生命周期与省钱操作（digest-pg）

> 背景：`db-f1-micro` **不在 GCP 免费层内**，运行状态约 **$10/月**（计算 + 10GB SSD）。
> 2027-02-18 赠金到期后，如果实例还在跑，就会**从银行卡真实扣费**。

## 三种状态与成本

| 状态 | 命令 | 计算费 | 存储费 | 说明 |
|---|---|---|---|---|
| **运行** | `--activation-policy ALWAYS` | ~$8.3/月 | ~$1.7/月 | 正常服务 |
| **停止** | `--activation-policy NEVER` | **$0** | ~$1.7/月 | 数据保留，随时可启 |
| **删除** | `gcloud sql instances delete` | $0 | **$0** | 先导出备份！ |

## 暂停（省钱但保留数据）

```bash
gcloud sql instances patch digest-pg --activation-policy NEVER --quiet
gcloud sql instances describe digest-pg --format="value(state)"   # STOPPED
```

**暂停后的影响**：
- Cloud Run 的 `/api/top` 查库会失败 → **自动回退到 GCS 快照**（响应里 `source` 变成 `gcs`），服务不中断
- `/api/db` 会返回 503（预期）
- 收藏功能（写路径）**不可用**
- 自动备份会失败（备份需要实例运行）

## 恢复

```bash
gcloud sql instances patch digest-pg --activation-policy ALWAYS --quiet
# 启动约需 1–3 分钟，之后 /api/db 自动恢复
```

## 删除前务必备份

```bash
# 1. 导出到 GCS（推荐 SQL 全量）
gcloud sql export sql digest-pg gs://luozawa-top-data/db-backup/digest-$(date +%F).sql \
  --database=digest --quiet

# 2. 删除实例（30 天内可在控制台恢复？注意：Cloud SQL 实例删除不可恢复，只有备份可恢复）
gcloud sql instances delete digest-pg --quiet
```

## 到期处理时间线

| 时间 | 动作 |
|---|---|
| 2027-02-10 | 到期前提醒（已设置）→ 决定续用还是删除 |
| 2027-02-18 | 赠金归零，开始真实计费 |
| 之后 | 若实例还在：约 **$10/月**从卡里扣 |

## 相关命令速查

```bash
# 看实例状态与规格
gcloud sql instances list --format="value(name,state,tier,region)"

# 看当前账单
# https://console.cloud.google.com/billing/014EC5-4ABAE4-9EA846

# 确认没有未挂载的静态 IP（会额外收费）
gcloud compute addresses list --format="value(name,status)"
```
