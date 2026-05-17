# 生产旧研究数据清理计划

## 目标

释放 124 生产服务器中旧 feature/control 研究链路占用的数据库空间，同时保留当前暗流 v2 主路径、原始 payload 证据和恢复能力。

## 本次清理范围

清理 PostgreSQL 表数据：

- `feature_labels`
- `feature_events`

清理前先备份到 `/opt/hfd_backups/legacy-feature-cleanup-<timestamp>/`。

## 本次不清理

- `signal_snapshots`
- `darkflow_interactions`
- `darkflow_zones`
- `trade_candidates`
- `shadow_paper_trades`
- `paper_trades`
- `experiment_runs`
- `hfd_hfd_raw_payloads` Docker volume
- `hfd_hfd_postgres` Docker volume
- Alembic migrations

## 前置保护

`experiment-worker` 必须停止旧链路写入：

- `--no-feature-research`
- `--no-research-reports`
- `--no-shadow-paper`

暗流 v2 由 `darkflow-worker` 继续维护，不依赖旧 `feature_events` / `feature_labels` 作为主决策路径。

## 恢复方式

如果需要恢复旧 feature 数据，在 124 上执行：

```bash
cd /opt/hfd-git.tmp
docker compose exec -T postgres pg_restore -U hfd -d hfd --clean --if-exists /backup/path/legacy_feature_tables.dump
```

恢复前应先停止会写入相关表的任务，避免恢复期间产生并发写入。

## 验收

- `/health` 返回 `ok`。
- HFD 当前容器继续运行。
- `feature_events` 和 `feature_labels` 行数为 0。
- 根盘可用空间明显增加。
- `darkflow-worker` 仍在运行，`LIVE_TRADING_ENABLED=false`，`TRADING_GATEWAY=disabled`。
