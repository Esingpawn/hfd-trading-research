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

## Raw Payload 冷归档

2026-05-17 已将 `hfd_hfd_raw_payloads` 中较旧的在线 raw payload 目录归档：

- 已归档在线目录：`/var/lib/docker/volumes/hfd_hfd_raw_payloads/_data/2026/05/{05..14}`
- 保留在线热目录：`/var/lib/docker/volumes/hfd_hfd_raw_payloads/_data/2026/05/{15..17}`
- 归档位置：`/opt/hfd_backups/raw-payload-archive-20260517T155522Z/raw_payloads_2026-05-05_to_2026-05-14.tar.gz`
- 校验文件：同目录 `.sha256`

恢复示例：

```bash
cd /var/lib/docker/volumes/hfd_hfd_raw_payloads/_data/2026/05
sha256sum -c /opt/hfd_backups/raw-payload-archive-20260517T155522Z/raw_payloads_2026-05-05_to_2026-05-14.tar.gz.sha256
tar -xzf /opt/hfd_backups/raw-payload-archive-20260517T155522Z/raw_payloads_2026-05-05_to_2026-05-14.tar.gz
```

注意：归档包当前仍保留在 124 本机，因此它降低了在线热数据体积，但不会释放这 8.2G 的整机占用。若要继续释放空间，需要先把归档迁移到 154、本地冷备份盘或对象存储，校验通过后再删除 124 本机归档包。
