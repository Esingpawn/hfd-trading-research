# HFD Trading Research System

暗流 Pro 信号研究与纸上交易系统。当前版本先做数据采集和信号快照，不接真实交易。

## 当前范围

- 采集 9 个币种：`BTC`、`ETH`、`SOL`、`BNB`、`LINK`、`TON`、`DOGE`、`HYPE`、`ZEC`
- 采集 3 个周期：短期 `30m`、中期 `1h`、长期 `4h`
- 保存 HFD Pro 原始指标响应
- 保存价格快照
- 提供 FastAPI 健康检查和最近快照查询
- 提供 CLI 初始化数据库和执行采集

## 快速开始

```powershell
cd D:\OneDrive\桌面\HFD
python -m app.cli init-db
python -m app.cli collect --coins BTC ETH --timeframes short --indicators smart_money_cost --dry-run
python -m app.cli collect --coins BTC ETH --timeframes short --indicators smart_money_cost
python -m app.cli collect-scoring-core --coins BTC ETH --timeframes short mid long --dry-run
python -m app.cli collect-loop --coins BTC ETH --timeframes short --indicators smart_money_cost --interval-seconds 1800 --max-runs 2
python -m app.cli backtest --coin BTC --timeframe short --limit-zones 50
python -m app.cli backtest-batch --coins BTC ETH SOL BNB LINK TON DOGE HYPE ZEC --timeframes short mid long --limit-zones 50
python -m app.cli evaluate --coin BTC --dry-run
python -m app.cli paper-scan --coins BTC ETH --dry-run
python -m app.cli paper-mark
uvicorn app.main:app --reload
```

## 日常推荐流程

普通使用不需要记上面所有底层命令，优先用脚本：

```powershell
cd D:\OneDrive\桌面\HFD
.\scripts\start-system.ps1
.\scripts\update-data.ps1
.\scripts\status.ps1
.\scripts\maintain-db.ps1 -Indexes -Checkpoint -Optimize
```

- `start-system.ps1`：初始化数据库，启动 FastAPI 面板，启动分层采集循环，并写入 PID 状态。分层采集会每轮刷新评分核心，并按周期补齐全量暗流研究指标。
- `update-data.ps1`：手动补齐 9 个币种、3 个周期的评分核心和全量研究数据。
- `status.ps1`：查看服务、采集循环、最近采集、下次采集、数据覆盖和 Telegram 状态。
- `maintain-db.ps1`：查看数据库体积、原始 payload 体积、缺失索引，并可执行 SQLite checkpoint / optimize。

面板地址：

```text
http://127.0.0.1:8000/dashboard
```

默认数据库是本地 SQLite：

```text
sqlite+aiosqlite:///./data/hfd.db
```

后续可通过环境变量切到 PostgreSQL：

```powershell
$env:DATABASE_URL="postgresql+psycopg://user:password@localhost:5432/hfd"
```

## API

- `GET /health`
- `GET /system/summary`
- `GET /system/runtime`
- `GET /system/storage`
- `POST /system/storage/indexes`
- `POST /system/storage/checkpoint?truncate=true`
- `POST /system/storage/optimize`
- `GET /config/universe`
- `GET /snapshots?limit=20`
- `GET /data/completeness`
- `POST /collect/run`
- `POST /collect/scoring-core`
- `GET /tasks`
- `POST /tasks/enqueue`
- `GET /decisions`
- `POST /paper/scan`
- `POST /paper/mark`
- `GET /paper/trades`
- `POST /backtests/batch`
- `GET /backtests/latest`
- `GET /telegram/status`
- `GET /telegram/updates`
- `POST /telegram/send`
- `GET /trading/safety`
- `PATCH /trading/safety`
- `POST /trading/orders`
- `GET /trading/orders`
- `GET /trading/audit`
- `GET /dashboard`

## Telegram 接入

不要把机器人 token 写进代码。复制 `.env.example` 为 `.env`，填入：

```text
TELEGRAM_BOT_TOKEN=你的机器人 token
TELEGRAM_CHAT_ID=你的 chat_id
```

获取 `chat_id` 的流程：

1. 在 Telegram 给机器人发送 `/start`
2. 执行：

```powershell
python -m app.cli telegram updates
```

3. 从输出里的 `chat_id` 复制到 `.env`
4. 测试发送：

```powershell
python -m app.cli telegram send --text "HFD 系统测试消息"
```

也可以在 `http://127.0.0.1:8000/dashboard` 里点击 `获取 TG chat_id` 和 `TG 测试`。

## 常用命令

采集全部 9 个币种、3 个周期、核心指标前，建议先 dry-run：

```powershell
python -m app.cli collect --dry-run
```

补齐多信号评分所需的关键指标：

```powershell
python -m app.cli collect-scoring-core --coins BTC ETH SOL BNB LINK TON DOGE HYPE ZEC --timeframes short mid long
```

定时采集示例，每 30 分钟跑一次：

```powershell
python -m app.cli collect-loop --interval-seconds 1800
```

推荐的常驻采集方式是分层循环：评分核心每 30 分钟刷新一次；全量研究指标按短期 30 分钟、中期 60 分钟、长期 4 小时刷新，避免面板只显示历史覆盖而实际新鲜覆盖不足。

```powershell
python -m app.cli collect-tiered-loop --core-interval-seconds 1800 --research-short-interval-seconds 1800 --research-mid-interval-seconds 3600 --research-long-interval-seconds 14400
```

历史回测初筛示例：

```powershell
python -m app.cli backtest --coin BTC --timeframe short --stop-pct 0.01 --target-pct 0.02
```

注意：`backtest` 使用暗流 Pro 当前返回的历史数据，只能做策略初筛，不能直接证明实盘可用。

批量历史初筛：

```powershell
python -m app.cli backtest-batch --coins BTC ETH SOL BNB LINK TON DOGE HYPE ZEC --timeframes short mid long --limit-zones 50
```

纸上交易扫描：

```powershell
python -m app.cli evaluate --coin BTC --dry-run
python -m app.cli paper-scan --coins BTC ETH --dry-run
python -m app.cli paper-scan --coins BTC ETH
python -m app.cli paper-mark
```

简易面板：

```text
http://127.0.0.1:8000/dashboard
```

## 维护说明

后续迭代前建议先阅读：

```text
docs/project-handoff.md
docs/data-quality-plan.md
docs/architecture-upgrade-plan.md
docs/linux-deployment.md
```

Linux 服务器部署优先使用 Docker Compose：

```bash
bash scripts/linux/bootstrap.sh
bash scripts/linux/status.sh
bash scripts/linux/logs.sh api
```

Docker Compose 中包含 `task-worker`，用于消费 Redis 里的 `task_runs`。可以通过 API 入队：

```bash
curl -X POST "http://127.0.0.1:8000/tasks/enqueue?task_name=signals.backfill&limit=500"
curl -X POST "http://127.0.0.1:8000/tasks/enqueue?task_name=paper.scan&coins=BTC&coins=ETH&notify=true"
```

项目文件按 UTF-8 保存。Windows PowerShell 默认编码有时会把中文显示成乱码，读取中文文件时优先使用：

```powershell
Get-Content -Raw -Encoding UTF8 README.md
```

## 重要边界

当前系统只做研究、纸上交易和交易安全网关准备。`LIVE_TRADING_ENABLED=false` 且 `TRADING_GATEWAY=disabled` 是默认配置，真实交易所网关未接入前，实盘订单会被拒绝或阻断并写入审计日志。历史数据回测只能用于策略初筛，最终必须依赖实时快照验证。


