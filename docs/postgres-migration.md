# SQLite 到 PostgreSQL 迁移说明

## 1. 前提

迁移前必须先备份当前 SQLite 数据库和 WAL 文件：

```bash
cp data/hfd.db data/backups/hfd-before-postgres.db
cp data/hfd.db-wal data/backups/hfd-before-postgres.db-wal 2>/dev/null || true
```

目标 PostgreSQL 必须先执行：

```bash
alembic upgrade head
```

## 2. Dry-run

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --source-url sqlite+aiosqlite:///./data/hfd.db \
  --target-url postgresql+psycopg://hfd:password@localhost:5432/hfd \
  --dry-run
```

Dry-run 只统计每张表将迁移的行数，不写入 PostgreSQL。

## 3. 正式迁移

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --source-url sqlite+aiosqlite:///./data/hfd.db \
  --target-url postgresql+psycopg://hfd:password@localhost:5432/hfd \
  --batch-size 250
```

## 4. 分表迁移

```bash
python scripts/migrate_sqlite_to_postgres.py \
  --target-url postgresql+psycopg://hfd:password@localhost:5432/hfd \
  --only collection_runs signal_snapshots price_snapshots
```

## 5. 验证

迁移后运行：

```bash
DATABASE_URL=postgresql+psycopg://hfd:password@localhost:5432/hfd python -m app.cli storage-health
DATABASE_URL=postgresql+psycopg://hfd:password@localhost:5432/hfd python -m pytest -q
```

确认：

- 表行数符合预期。
- `/system/summary` 可读。
- `/market/overview` 可读。
- worker 能正常写入新数据。

## 6. 注意

当前迁移脚本使用 ORM merge，优先保证安全和可重复执行，不追求最快速度。迁移 6GB raw payload 会比较慢，后续 raw payload 外置后需要重新设计迁移策略。
