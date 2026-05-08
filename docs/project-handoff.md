# HFD 项目接手与迭代说明

本文档用于后续迭代时快速恢复上下文，重点记录当前系统边界、模块职责、运行方式和优先级。

## 项目定位

HFD 当前是暗流 Pro 信号研究与纸上交易系统，不做真实下单。系统目标是把 HFD Pro 指标转为可记录、可复盘、可评分、可验证的交易决策流。

核心原则：先保存实时快照，再做策略验证；先纸上交易，再模拟盘；实盘默认关闭。

## 当前技术栈

- Python 3.11+
- FastAPI + Uvicorn
- SQLAlchemy async ORM
- SQLite 默认本地库：`sqlite+aiosqlite:///./data/hfd.db`
- 可通过 `DATABASE_URL` 切换 PostgreSQL
- 单页 HTML 面板：`app/web/dashboard.html`
- PowerShell 运维脚本：`scripts/*.ps1`

## 核心模块

- `app/constants.py`：币种池、周期映射、核心指标配置。
- `app/models.py`：数据库表模型。
- `app/hfd/client.py`：HFD Pro API 和价格接口客户端。
- `app/services/collector.py`：采集价格与指标快照。
- `app/services/completeness.py`：数据覆盖和过期判断。
- `app/services/strategy.py`：多周期成本带策略评分。
- `app/services/risk.py`：止盈止损、R 倍数、执行区间计算。
- `app/services/paper.py`：纸上交易扫描、连续确认、开仓和平仓标记。
- `app/services/backtest.py`：静态历史回测初筛。
- `app/services/backtest_batch.py`：多币种多周期批量回测。
- `app/services/telegram.py`：Telegram 状态、消息发送、chat_id 获取。
- `app/api/routes.py`：FastAPI 接口和面板数据聚合。
- `app/cli.py`：命令行入口。

## 数据模型

- `SignalSnapshot`：HFD 指标原始响应和摘要。
- `PriceSnapshot`：交易对价格快照。
- `CollectionRun`：一次采集任务状态。
- `StrategyDecision`：策略评分和风控输出。
- `PaperTrade`：纸上交易生命周期。
- `BacktestRun`：批量回测结果。

## 运行方式

常规启动：

```powershell
cd D:\OneDrive\桌面\HFD
.\scripts\start-system.ps1
```

`start-system.ps1` 会启动或复用三个运行单元：FastAPI 面板服务、核心采集循环、纸上交易循环。纸上交易循环只在发现新的 `completed` 采集批次后执行 `paper-mark` 和 `paper-scan`，默认不会处理脚本启动前已经存在的采集批次，避免重启后重复扫描同一批数据。

补齐数据：

```powershell
.\scripts\update-data.ps1
```

查看状态：

```powershell
.\scripts\status.ps1
```

状态接口 `/system/runtime` 会返回 `collector` 和 `paper_loop` 的 PID、日志路径、最近日志行和诊断结果；服务器部署后应把该接口或 `scripts/status.ps1` 纳入巡检。

面板地址：

```text
http://127.0.0.1:8000/dashboard
```

## 数据质量闸门

- 过期快照不参与评分。
- 长期方向缺失或过期时禁止开仓。
- 评分核心指标缺失时禁止开仓。
- 固定风控兜底不能直接视为高置信开仓。
- 全量研究覆盖不足时允许观察和纸上验证，但不能升级为实盘依据。

评分核心指标：

- `smart_money_cost`
- `liq_heatmap`
- `cross_exchange_resonance`
- `imbalance`
- `trend_exhaustion`

全量研究指标：

- `trend_price`
- `inst_vwap`
- `liquidation_fuel`
- `liquidity_sweep`
- `inst_volume_profile`
- `hvn_nodes`
- `micro_poc`

## Windows 编码注意

项目文件按 UTF-8 保存。PowerShell 某些命令在默认编码下可能把中文显示成乱码，但文件内容本身是正常的。读取中文文件时优先使用：

```powershell
Get-Content -Raw -Encoding UTF8 <path>
```

如果需要改善终端显示，可在当前 PowerShell 会话执行：

```powershell
chcp 65001
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::UTF8
```

## 验证基线

每次修改后至少运行：

```powershell
python -m pytest -q
python -m compileall app tests
```

当前接手基线：`python -m pytest -q` 全量通过，最新纸上交易循环增强后为 `44 passed`。

## 后续优先级

1. 建立版本控制或至少保留变更快照，当前目录不是 Git 仓库。
2. 服务器化运行：进程守护、环境变量管理、日志轮转、备份恢复和健康检查告警。
3. 完善纸上交易统计：手续费、滑点、ROI、Profit Factor、最大回撤、按币种/周期拆分。
4. 增加快照回放能力，用真实保存的快照替代静态历史接口回测。
5. 抽象模拟盘网关，但保持实盘交易开关关闭。

## 迭代纪律

- 策略参数变更必须记录版本。
- 每次只改一个主要策略变量，避免无法复盘。
- 接模拟盘前必须有足够纸上交易样本。
- 实盘相关代码必须默认关闭，并经过人工确认和风控闸门。
