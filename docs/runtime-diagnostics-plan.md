# 运行日志与状态诊断增强 Autoplan

生成时间：2026-05-07

## Plan Summary

目标是在不改变交易策略逻辑的前提下，增强 HFD 系统的运行诊断能力，让采集循环、服务状态、数据新鲜度、日志错误和 Telegram 状态可以被 CLI、API 和面板一致地解释。

本计划不接入实盘、不修改策略评分参数、不改变纸上交易开仓条件。

## Scope

### In Scope

- 增强 `/system/runtime` 或新增 `/system/diagnostics`，输出统一健康等级和问题列表。
- 让 `scripts/status.ps1` 显示可操作的异常原因和最近错误日志。
- 让面板状态区展示采集循环、数据过期、日志错误和 Telegram 异常的清晰状态。
- 增加针对诊断聚合逻辑的单元测试。
- 保持现有 `start-system.ps1`、`update-data.ps1` 兼容。

### NOT In Scope

- 不接交易所模拟盘或实盘。
- 不修改 `strategy.py` 的评分权重和开仓门槛。
- 不引入 Celery、APScheduler、Prometheus 或外部监控系统。
- 不重构为前后端分离应用。
- 不迁移数据库。

## What Already Exists

- `app/api/routes.py` 已有 `/system/runtime`，可返回服务 PID、采集循环 PID、最近采集、日志最后一行和缓存状态。
- `scripts/start-system.ps1` 已写入 `data/runtime/*.json` 和 `data/runtime/*.pid`。
- `scripts/status.ps1` 已调用 `/system/runtime`、`/data/completeness` 和 `/telegram/status`。
- `app/web/dashboard.html` 已展示 Telegram、采集循环、覆盖率、最近采集和快照数量。
- `data/logs/*.log` 已保存服务与采集循环日志。

## Recommended Implementation

1. 在 `app/api/routes.py` 提取诊断组装函数，或新增小模块 `app/services/diagnostics.py`，避免路由文件继续膨胀。
2. 诊断 payload 增加：`overall_status`、`issues`、`actions`、`collector_age_seconds`、`stale_slots`、`last_error_line`、`log_sizes`。
3. 保持 `/system/runtime` 向后兼容，把新字段附加进去；如字段较多，再新增 `/system/diagnostics`。
4. 更新 `scripts/status.ps1`，将异常分为：服务不可达、采集未运行、最近采集失败、评分核心缺失、评分核心过期、Telegram 异常。
5. 更新面板状态区，只展示高信号摘要，详细日志仍通过 API/脚本查看。
6. 增加测试：诊断等级计算、日志不存在、错误日志为空、采集过期、数据缺失、PID 不存在。

## Phase 1: CEO Review

### Premise Challenge

当前前提是“先补诊断，再做纸上交易统计”。这个前提成立。没有可靠状态诊断时，纸上交易结果会混入采集缺口、过期数据、后台进程静默退出等工程噪声，后续复盘会误把工程问题当成策略问题。

### Auto Decisions

- 选择增强现有本地诊断，而不是引入完整监控栈。原因：当前是本地研究系统，外部监控会提高维护成本。
- 选择先保持 `/system/runtime` 兼容。原因：面板和脚本已经依赖它，破坏接口没有必要。
- 选择把诊断规则做成纯函数。原因：测试成本低，后续接模拟盘前也能复用。

### User Challenges

无。当前方向与项目阶段一致。

### Taste Decisions

无强制口味选择。是否新增 `/system/diagnostics` 可以在实现时根据字段膨胀程度决定。

## Phase 2: Design Review

UI scope 存在，但很小，集中在面板状态区。

### Design Findings

- 面板不应该塞入完整日志。用户需要先看到“能不能信这次数据”，再决定是否看细节。
- 状态颜色建议保持三态：正常、警告、错误。避免把所有异常都显示成红色。
- 文案应输出动作，而不是只输出状态。例如“采集循环未运行，执行 start-system.ps1”优于“未运行”。

### Design Score

- 信息层级：7/10
- 可操作性：8/10
- 视觉风险：低

## Phase 3: Engineering Review

### Architecture

```text
scripts/start-system.ps1
        |
        v
data/runtime/*.json + data/logs/*.log
        |
        v
app/services/diagnostics.py  <--- app/services/completeness.py
        |
        v
app/api/routes.py (/system/runtime or /system/diagnostics)
        |
        +--> scripts/status.ps1
        |
        +--> app/web/dashboard.html
```

### Code Quality

- `app/api/routes.py` 当前已经接近 800 行。新增复杂诊断规则时应提取到服务层或至少提取纯函数。
- PowerShell 脚本应继续只做编排和展示，不承载复杂业务判断。
- 日志读取应保持尾部读取，不要全量读取大日志。

### Test Diagram

| Codepath | Expected Test |
|---|---|
| collector running + fresh data | overall_status = ok |
| collector stopped | issue includes collector_not_running |
| latest collection has errors | issue includes collection_errors |
| scoring coverage missing | issue includes scoring_missing |
| scoring slots stale | issue includes scoring_stale |
| stderr log has last line | issue includes last_error_line |
| log file missing | no crash, issue optional |
| Telegram configured but failing | warning, not system critical |

### Performance

- 当前 `/system/runtime` 只读小 JSON 和日志尾部，成本低。
- `/data/completeness` 已有 300 秒缓存。诊断接口应复用缓存，不重复做重查询。
- 面板刷新不应频繁触发采集或回测。

### Failure Modes Registry

| Failure Mode | Severity | Mitigation |
|---|---|---|
| PID 文件存在但进程已退出 | High | runtime 中以实际 PID 检查为准 |
| 采集循环运行但连续报错 | High | 读取 collection_runs errors 和 stderr 尾行 |
| 数据覆盖 100% 但已过期 | High | 诊断必须同时看 stale_slots |
| 日志过大导致接口慢 | Medium | 只读取尾部固定字节 |
| Telegram 异常误判系统不可用 | Low | 降级为通知告警，不阻断核心运行 |

## Phase 3.5: DX Review

Developer-facing scope 存在：脚本和 README 维护说明。

### Developer Journey

| Stage | Current | Target |
|---|---|---|
| Start | `start-system.ps1` | unchanged |
| Status | `status.ps1` | shows issue + action |
| Inspect API | `/system/runtime` | stable diagnostic fields |
| Inspect UI | dashboard | concise health state |
| Debug logs | manual log open | last useful error surfaced |

### TTHW

当前本地启动路径约 2-5 分钟，目标保持不变。诊断增强不应增加启动步骤。

### DX Score

- CLI clarity：7/10 -> target 9/10
- Error actionability：5/10 -> target 8/10
- Docs findability：7/10 -> target 8/10

## Cross-Phase Themes

**Theme: 状态必须可操作。** CEO、Design、Eng、DX 都指向同一个要求：不要只显示“异常”，要显示异常来源和下一步动作。

**Theme: 保持轻量。** 当前项目阶段不适合引入外部监控系统，应先把本地诊断打磨清楚。

## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| 1 | CEO | 先做本地诊断增强 | Auto | Completeness | 采集和数据状态不可靠会污染纸上交易结论 | 先做交易统计 |
| 2 | CEO | 不引入外部监控栈 | Auto | Simplicity | 当前是本地研究系统，Prometheus/Celery 会增加运维面 | 直接上监控平台 |
| 3 | Eng | 诊断规则提取为纯函数/服务层 | Auto | Testability | 可测试、可复用，避免 routes.py 继续膨胀 | 全写在路由函数里 |
| 4 | Eng | 保持 `/system/runtime` 向后兼容 | Auto | Compatibility | 面板和脚本已依赖现有字段 | 破坏式替换接口 |
| 5 | Design | 面板只展示摘要和动作 | Auto | Usability | 状态区应快速回答能否信任当前数据 | 在面板堆完整日志 |
| 6 | DX | `status.ps1` 输出问题和建议动作 | Auto | Operability | 命令行是日常维护最快入口 | 只输出布尔状态 |

## Deferred To TODOS

- 外部监控栈接入：等系统进入模拟盘或长期运行阶段再评估。
- 模拟盘网关：依赖纸上交易统计和诊断稳定后再推进。
- 数据库迁移：当前 SQLite 足够本地研究，PostgreSQL 留作后续部署选项。

## Final Gate Recommendation

建议批准该计划，并作为下一次实现任务的范围边界。

