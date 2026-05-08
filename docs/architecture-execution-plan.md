# HFD 架构升级执行计划

## 1. 当前完成状态

已完成：

- GitHub 仓库与基础提交。
- 数据库健康与维护接口。
- Linux / Docker Compose 部署基础。
- PostgreSQL 与 Redis 容器编排基础。
- `collector-worker`、`paper-worker`、`experiment-worker` 容器入口。
- 跨平台 CLI 维护命令：`storage-health`、`storage-maintain`、`experiment-loop`。
- 架构升级规划文档。

未完成：

- API 路由分包。
- CLI 分包。
- Application / Domain / Infrastructure 分层。
- Alembic migration。
- SQLite 到 PostgreSQL 的数据迁移脚本。
- raw payload 外置压缩存储。
- Redis cache / queue 实际接入。
- RQ / Celery / Arq / APScheduler 任务系统。
- PostgreSQL 分区与大表治理。
- 前端 Vite React 工程化。
- 结构化日志、监控指标和完整 Telegram 告警规则。
- 模拟盘 / 实盘安全网关。

## 2. 执行原则

1. 每一步只改变一个主要架构面。
2. 每一步必须保持测试通过。
3. 数据迁移类改动必须先有备份方案。
4. 策略行为不能在结构重构中被隐式改变。
5. 生产部署能力优先于 UI 美化。
6. 实盘能力默认关闭，直到风控门禁齐全。

## 3. 阶段 1：API / CLI 边界拆分

目标：降低继续迭代风险，先把大文件拆开。

任务：

- 拆分 `app/api/routes.py`。
- 建立 `app/api/deps.py` 保存 session dependency。
- 建立 `app/api/routes/` 分包。
- 按领域拆成：`system.py`、`market.py`、`collection.py`、`paper.py`、`signals.py`、`backtests.py`、`telegram.py`、`dashboard.py`。
- 保持所有 HTTP path 和响应结构不变。
- 拆分 `app/cli.py`，将命令处理迁移到 `app/cli_commands/`。

验收：

- `python -m pytest -q` 通过。
- `python -m py_compile app` 相关文件通过。
- 现有 dashboard 可访问。
- API 路径不变。

## 4. 阶段 2：Application / Domain / Infrastructure 分层

目标：让核心业务逻辑不依赖 FastAPI、CLI 或具体存储实现。

任务：

- 新增 `app/application/` 用例层。
- 新增 `app/domain/` 领域层。
- 新增 `app/infrastructure/` 基础设施层。
- 将 HFD client、Telegram client、raw store、cache/queue adapter 放到基础设施层。
- 将策略评分、风险、纸上交易、信号实验逐步迁入领域层或用例层。

验收：

- API 和 CLI 只调用 application service。
- 核心策略测试不依赖 FastAPI。
- 旧路径保留兼容或提供迁移说明。

## 5. 阶段 3：Alembic 与 PostgreSQL 迁移

目标：服务器数据结构可版本化管理。

任务：

- 引入 Alembic。
- 生成当前 schema baseline。
- 补 PostgreSQL 本地/容器测试流程。
- 编写 SQLite 到 PostgreSQL 迁移脚本。
- 补迁移验证脚本。

验收：

- 空 PostgreSQL 可以通过 migration 建表。
- 当前 SQLite 数据可以迁移到 PostgreSQL 测试库。
- 迁移后核心接口可读。

## 6. 阶段 4：raw payload 外置压缩

目标：解决数据库体积增长问题。

任务：

- 新增 raw payload store 抽象。
- 本地实现 gzip 或 zstd 文件存储。
- `signal_snapshots` 增加 `raw_payload_uri`、`raw_payload_sha256`、`raw_payload_bytes`、`raw_payload_compression`。
- 新采集数据默认外置 raw payload。
- 旧数据迁移为压缩文件。

验收：

- 新快照不再把大 raw payload 写入数据库。
- 可通过引用恢复原始 payload。
- 存储健康接口能展示 raw store 体积。

## 7. 阶段 5：Redis 与任务系统

目标：替代容器内长循环为可观测任务系统。

任务：

- 接入 Redis cache。
- 接入 Redis lock，避免重复采集同一槽位。
- 选择 RQ / Celery / Arq / APScheduler 之一。
- 建立任务表和任务状态 API。
- API 发起任务，worker 执行任务。

验收：

- 采集、纸上交易、实验回填都有任务记录。
- worker 重启不会重复处理已完成任务。
- `/system/runtime` 展示任务队列与 worker 状态。

## 8. 阶段 6：实验治理和权重版本化

目标：回答哪些信号有效、哪些是噪音。

任务：

- 建立 `experiment_runs`。
- 建立标准化 `feature_events`。
- 建立 `weight_versions`。
- 区分训练样本、验证样本、纸上交易样本。
- 权重升级需要样本数、胜率、收益风险比、回撤、分层表现。

验收：

- 每次权重变更可追溯。
- 实验指标不能绕过治理直接参与开仓。
- dashboard 能展示信号有效性与样本置信度。

## 9. 阶段 7：前端工程化

目标：支持复杂面板长期维护。

任务：

- 新建 Vite React 前端。
- 拆分市场、信号、实验、权重、纸上交易、系统健康页面。
- 接入 ECharts / Lightweight Charts。
- 保留旧 dashboard 到新面板覆盖核心功能。

验收：

- 新面板覆盖旧面板核心能力。
- 不再继续扩展单文件 HTML。

## 10. 阶段 8：模拟盘 / 实盘安全网关

目标：从研究系统升级为受控交易系统。

任务：

- 模拟盘交易网关。
- 订单状态机。
- 幂等键和重试策略。
- kill switch。
- 风险限额。
- 人工确认模式。
- 审计日志。

验收：

- 实盘默认关闭。
- 未通过风控门禁不能下单。
- 所有交易动作可追溯。

## 11. 当前进度

阶段 1 已完成：

- API 路由已从单个 `app/api/routes.py` 拆分为领域路由分包，并保留原有 HTTP path 与响应结构。
- CLI 入口已薄化，命令执行逻辑迁移到 `app/cli_commands/runner.py`，共享 CLI helper 迁移到 `app/cli_commands/`。

阶段 2 已启动：已建立 Application / Domain / Infrastructure 分层骨架，并将存储健康/维护作为第一条 application 用例迁入 `app/application/storage.py`。

阶段 3 已完成基础版：

- 已加入 Alembic 配置和当前 schema 的 baseline migration。
- Docker 初始化改为 `alembic upgrade head`。
- 已新增 SQLite 到 PostgreSQL 的批量迁移脚本与迁移说明。

阶段 4 已完成基础版：

- `signal_snapshots` 已增加 raw payload 外置引用字段。
- 新增本地 gzip raw payload store。
- 采集器支持通过 `EXTERNALIZE_RAW_PAYLOADS=true` 将新采集的 signal raw payload 外置压缩保存。
- 存储健康接口已能统计外置 payload 引用和体积。

阶段 5 已完成基础版：

- 已增加 `task_runs` 任务记录表。
- 已增加 `/tasks` 和 `/tasks/enqueue` API。
- 已增加 Redis queue / Null queue 适配器。
- 无 Redis 时任务可记录为 `recorded`，有 Redis 时可推入 Redis list。

下一步执行阶段 6：实验治理和权重版本化基础表与接口。
