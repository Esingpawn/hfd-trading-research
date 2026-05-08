# HFD 架构升级规划

## 1. 结论

HFD 需要进行一次架构级升级，但不需要因为后续部署到 Linux 服务器而替换 FastAPI。

推荐路线：保留 `FastAPI + SQLAlchemy + Python` 作为核心后端技术栈，将系统从当前的“Windows 本地脚本 + SQLite + 单文件面板”升级为“Linux 服务器 + Docker Compose + PostgreSQL + Redis + Worker + 外置原始数据存储”的模块化单体架构。

当前不要做微服务化。现阶段更重要的是明确数据边界、任务边界、实验治理边界和实盘安全边界。

## 2. 当前系统状态

当前系统已经具备这些能力：

- HFD Pro 指标采集。
- 9 个币种、3 个周期的数据覆盖。
- 评分核心指标与全量研究指标分层采集。
- 策略评分、风险计算、纸上交易扫描。
- 信号观察、指标实验、权重治理。
- FastAPI 接口与单文件 dashboard。
- Telegram 状态与告警能力。
- SQLite 存储健康检查和维护接口。
- GitHub 仓库与基础测试集。

当前已经暴露的架构压力：

- SQLite 数据库已经超过 6GB，主要由 `signal_snapshots.raw_payload` 构成。
- 原始 HFD payload、摘要数据、查询数据混在一个数据库里。
- API 路由、CLI、dashboard 文件持续变大，边界开始模糊。
- Windows PowerShell 脚本适合本地开发，不适合作为 Linux 服务器生产运行方式。
- 采集、纸上交易、实验回填、维护任务需要稳定的 worker 和调度机制。
- 未来模拟盘和实盘需要审计、幂等、风控开关和人工确认流程。

## 3. 架构原则

1. 保留能工作的核心业务逻辑，不为了换技术而重写。
2. 先升级数据层和任务层，再升级 UI。
3. 原始数据与查询数据分离。
4. API 服务不直接承担长任务。
5. 所有策略、权重、实验、交易决策必须可追溯。
6. 实盘能力默认关闭，必须有独立风控门禁和人工确认。
7. 本地开发和服务器生产运行方式分离。
8. 先做模块化单体，不做过早微服务化。

## 4. 目标技术栈

| 层级 | 当前 | 目标 |
|---|---|---|
| 后端 API | FastAPI | 保留 FastAPI |
| ORM | SQLAlchemy async | 保留 SQLAlchemy，补 Alembic |
| 本地数据库 | SQLite | 本地可保留 SQLite，服务器使用 PostgreSQL |
| 大表/时间序列 | 无 | PostgreSQL 分区，后续可选 TimescaleDB |
| 原始 payload | SQLite JSON | gzip/zstd 压缩文件、MinIO 或 S3 |
| 缓存 | 进程内缓存 | Redis |
| 后台任务 | PowerShell 常驻脚本 | Worker + Scheduler |
| 部署 | 手动脚本 | Docker Compose，后续可选 systemd |
| 前端 | 单文件 HTML | 后续 Vite React |
| 日志 | 文件日志 | stdout/stderr + logrotate/journald |
| 告警 | Telegram | 保留 Telegram，补异常告警规则 |

## 5. 目标运行架构

```text
Linux Server
  Nginx / Caddy
    FastAPI API Server

  PostgreSQL
    structured tables
    latest state tables
    experiment tables
    paper trading tables

  Redis
    cache
    queue
    distributed locks

  Workers
    collector-worker
    paper-worker
    experiment-worker
    maintenance-worker

  Raw Payload Storage
    local volume / MinIO / S3

  Observability
    health APIs
    structured logs
    Telegram alerts
    backups
```

## 6. 目标代码分层

当前可以逐步演进为以下结构，不需要一次性重写：

```text
app/
  api/
    routes/
      market.py
      system.py
      paper.py
      signals.py
      backtests.py
      telegram.py
  application/
    collect_market_data.py
    evaluate_strategy.py
    run_paper_scan.py
    backfill_signal_outcomes.py
    govern_signal_weights.py
  domain/
    signals/
    strategy/
    risk/
    paper/
    experiments/
  infrastructure/
    db/
    hfd_client.py
    telegram_client.py
    raw_store.py
    cache.py
    queue.py
  workers/
    collector.py
    paper.py
    experiment.py
    maintenance.py
```

拆分顺序建议：

1. 先拆 `app/api/routes.py`。
2. 再拆 `app/cli.py`。
3. 然后把服务函数按 application/domain/infrastructure 分层。
4. 最后将 worker 从 CLI 中独立出来。

## 7. 数据层升级计划

### 7.1 PostgreSQL

服务器部署应使用 PostgreSQL 作为主数据库。

原因：

- 当前 SQLite 已超过 6GB，不适合长期高频写入和服务器多进程访问。
- PostgreSQL 更适合索引、分区、并发写入、备份恢复和后续查询分析。
- SQLAlchemy 已经为切换 PostgreSQL 留出基础。

必须补充：

- Alembic migration。
- PostgreSQL 初始化脚本。
- 本地 SQLite 到 PostgreSQL 的迁移脚本。
- 数据库备份与恢复脚本。
- 数据库健康检查和慢查询观察。

### 7.2 原始 payload 外置

当前最大问题是 `signal_snapshots.raw_payload` 体积过大。后续需要将原始 HFD 响应从数据库中剥离。

目标方式：

```text
database:
  signal_snapshots.id
  symbol
  timeframe
  indicator
  collected_at
  summary_payload
  raw_payload_uri
  raw_payload_sha256
  raw_payload_bytes
  raw_payload_compression

storage:
  data/raw_payloads/YYYY/MM/DD/{symbol}/{timeframe}/{indicator}/{snapshot_id}.json.zst
```

短期可用本地磁盘压缩文件；服务器稳定后可升级 MinIO 或 S3。

### 7.3 查询读模型

为了避免面板和策略频繁扫描快照表，需要新增读模型：

- `latest_signal_states`
- `latest_price_states`
- `collection_slot_states`
- `indicator_coverage_states`
- `strategy_score_snapshots`

这些表用于 dashboard、状态接口和策略快速读取。原始快照表用于审计和复盘。

## 8. 任务与调度升级计划

### 8.1 当前问题

当前 Windows 本地通过 PowerShell 启动：

- FastAPI server。
- 分层采集循环。
- 纸上交易循环。

这适合本地开发，但不适合 Linux 服务器长期运行。

### 8.2 目标方式

服务器使用 Docker Compose 管理进程：

- `api`: FastAPI API 服务。
- `collector-worker`: HFD 数据采集。
- `paper-worker`: 纸上交易扫描与持仓标记。
- `experiment-worker`: 信号回填、指标实验、权重治理。
- `maintenance-worker`: 数据库维护、备份、清理、健康检查。
- `postgres`: PostgreSQL。
- `redis`: 缓存、队列、锁。

任务系统可选方案：

| 方案 | 优点 | 缺点 | 建议 |
|---|---|---|---|
| APScheduler + 独立 worker | 简单，上手快 | 分布式能力弱 | 可作为过渡 |
| RQ + Redis | 简单稳定 | 调度能力需补 | 适合当前阶段 |
| Celery + Redis | 成熟，能力完整 | 配置复杂 | 中长期可选 |
| Arq + Redis | async 友好 | 生态较小 | 可评估 |

推荐：先使用 `RQ + Redis` 或 `APScheduler + Redis lock`，不要一开始引入过重的任务平台。

## 9. 实验治理升级计划

指标实验和权重治理需要从“统计报表”升级为“可追溯实验系统”。

需要新增或完善：

- `experiment_runs`: 每次实验的参数、样本窗口、版本、结果。
- `feature_events`: 从原始指标中提取出的标准化事件。
- `signal_labels`: 未来收益、MFE、MAE、命中状态。
- `weight_versions`: 权重版本、样本依据、生效时间、回滚状态。
- `strategy_versions`: 策略版本、参数、阈值、风险配置。
- `decision_audit_logs`: 每次策略决策的完整解释链。

关键规则：

- 实验指标不能直接进入开仓权重。
- 权重升级必须有样本数、胜率、收益风险比、回撤和分层表现。
- 训练样本、验证样本、纸上交易样本必须区分。
- 每次策略决策必须记录所使用的策略版本、权重版本和数据版本。

## 10. 纸上交易、模拟盘、实盘演进

阶段顺序：

1. 纸上交易。
2. 模拟盘网关。
3. 小额实盘观察。
4. 受限实盘自动化。

实盘之前必须完成：

- 订单幂等键。
- 交易状态机。
- 风控 kill switch。
- 单日最大亏损限制。
- 单币种最大风险敞口。
- 最大同时持仓数。
- 手续费、滑点、资金费率建模。
- 人工确认模式。
- 完整审计日志。
- Telegram 异常通知。
- 实盘默认关闭。

## 11. 前端升级计划

当前 `dashboard.html` 可以继续短期使用，但后续功能继续增加时需要升级为真正的前端项目。

推荐：`Vite + React + TypeScript`。

原因：

- 这是内部工具，不需要 Next.js 的 SEO 能力。
- Vite 轻量，适合 dashboard。
- React 生态适合复杂状态和图表。

页面拆分建议：

- 市场总览。
- 信号解释。
- 指标实验矩阵。
- 权重治理。
- 纸上交易。
- 回测与复盘。
- 系统健康。
- 设置。

前端不是第一优先级。优先级在数据层、任务层、实验治理之后。

## 12. Linux 服务器部署计划

### 12.1 推荐目录

```text
/opt/hfd/
  app/
  docker-compose.yml
  .env
  data/
    raw_payloads/
    backups/
  logs/
```

### 12.2 Docker Compose 服务

最小生产组合：

- `api`
- `collector-worker`
- `paper-worker`
- `experiment-worker`
- `postgres`
- `redis`

可选：

- `nginx` 或 `caddy`
- `minio`
- `maintenance-worker`

### 12.3 Linux 替代 PowerShell

PowerShell 脚本继续保留给 Windows 本地开发，但服务器上改为：

- `docker compose up -d`
- `docker compose logs -f api`
- `docker compose ps`
- `scripts/linux/*.sh`
- `systemd` 或 Docker restart policy。

核心操作应该沉淀到 Python CLI，避免 shell 绑定平台：

```bash
python -m app.cli init-db
python -m app.cli storage-health
python -m app.cli storage-maintain
python -m app.cli worker collector
python -m app.cli worker paper
python -m app.cli worker experiment
```

## 13. 运维与安全计划

必须补齐：

- `.env.example` 面向服务器部署。
- Docker secret 或受限 env file。
- 数据库每日备份。
- raw payload 定期归档。
- 日志轮转。
- 健康检查接口。
- Telegram 异常告警。
- 启动失败告警。
- 数据过期告警。
- worker 卡死告警。
- 数据库体积增长告警。

安全边界：

- 不提交 `.env`。
- 不提交数据库和 payload 数据。
- 实盘 API key 独立配置。
- 实盘交易默认关闭。
- 实盘启用需要显式环境变量和人工确认。

## 14. 分阶段实施路线

### 阶段 0：架构文档与边界冻结

目标：明确路线，避免后续无序迭代。

任务：

- 保存本规划。
- 补 README 链接。
- 明确 FastAPI 保留、PostgreSQL、Redis、Docker Compose、worker 的方向。
- 标记 PowerShell 为本地开发工具。

验收：

- 文档存在并纳入 Git。
- 后续实施以本文档为主线。

### 阶段 1：代码边界整理

目标：降低继续迭代的维护风险。

任务：

- 拆分 `app/api/routes.py`。
- 拆分 `app/cli.py`。
- 建立 `application/`、`domain/`、`infrastructure/` 目录。
- 保持测试通过。

验收：

- API 行为不变。
- CLI 行为不变。
- 全量测试通过。

### 阶段 2：PostgreSQL 与 Alembic

目标：服务器数据层可长期运行。

任务：

- 引入 Alembic。
- 生成当前 schema migration。
- 新增 PostgreSQL docker-compose 服务。
- 新增 SQLite 到 PostgreSQL 迁移脚本。
- 在本地验证 PostgreSQL 测试库。

验收：

- PostgreSQL 上能启动 API。
- 现有测试通过。
- 样本数据迁移成功。

### 阶段 3：raw payload 外置

目标：控制数据库体积增长。

任务：

- 新增 raw store 抽象。
- 支持本地 zstd/gzip 压缩文件。
- `signal_snapshots` 增加 raw payload 引用字段。
- 新采集数据默认外置 raw payload。
- 编写旧数据迁移工具。

验收：

- 新增快照不再把大 raw payload 直接写入数据库。
- 能通过引用恢复原始 payload。
- 存储健康接口能展示 raw store 体积。

### 阶段 4：Worker 与 Redis

目标：替代服务器上的常驻脚本。

任务：

- 引入 Redis。
- 建立任务表和任务状态接口。
- collector/paper/experiment worker 独立运行。
- API 只发起任务和查询任务状态。
- Linux/Docker Compose 启动多进程。

验收：

- 服务器可通过 Docker Compose 启动全部服务。
- worker 异常退出能自动重启。
- `/system/runtime` 能展示 worker 状态。

### 阶段 5：实验治理与权重版本

目标：知道哪些信号有效，哪些是噪音。

任务：

- 建立 experiment runs。
- 建立 feature events。
- 建立 weight versions。
- 加入训练/验证/纸上交易样本分离。
- 加入权重升级门槛。

验收：

- 每个权重调整有版本和证据。
- 每个信号有效性有样本分层。
- 实验指标不能绕过治理直接影响开仓。

### 阶段 6：Linux 服务器上线

目标：长期稳定运行。

任务：

- Dockerfile。
- docker-compose.yml。
- `.env.production.example`。
- Linux 启停脚本。
- 备份脚本。
- 日志轮转。
- Telegram 告警。
- 部署文档。

验收：

- 新服务器从空环境可完成部署。
- 数据库、payload、配置均可备份恢复。
- 系统异常能告警。

### 阶段 7：前端工程化

目标：让复杂功能可维护。

任务：

- 创建 Vite React 前端。
- 拆分 dashboard 页面。
- 图表组件化。
- API client 类型化。
- 保留旧 dashboard 到新面板稳定后再移除。

验收：

- 新面板覆盖旧面板核心功能。
- 复杂页面不再堆在单个 HTML 文件中。

### 阶段 8：模拟盘与实盘准备

目标：从研究系统走向受控交易系统。

任务：

- 模拟盘交易网关。
- 订单状态机。
- 幂等和重试。
- 风控 kill switch。
- 人工确认模式。
- 实盘 API key 隔离。
- 实盘前审计报告。

验收：

- 模拟盘稳定运行。
- 所有订单可追溯。
- 实盘默认关闭。
- 未通过风控门槛无法下单。

## 15. 明确不做

短期不做：

- 不换 FastAPI。
- 不上 Kubernetes。
- 不拆微服务。
- 不先重做 UI。
- 不把实验信号直接用于实盘。
- 不在 SQLite 中继续无限堆 raw payload。

## 16. 下一个推荐动作

下一步建议从阶段 1 开始：先整理代码边界，再进入 PostgreSQL 和 raw payload 外置。

推荐实施顺序：

1. 拆分 API routes。
2. 拆分 CLI。
3. 引入 Alembic。
4. 增加 Docker Compose 的 PostgreSQL 和 Redis。
5. 做 raw payload 外置。
6. 再做 worker。

这样每一步都能验证，不会一次性大爆炸式重写。
