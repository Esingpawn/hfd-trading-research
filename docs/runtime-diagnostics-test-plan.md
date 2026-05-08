# Runtime Diagnostics Test Plan

## Goal

验证运行诊断增强不会改变策略逻辑，并且能稳定解释服务、采集、数据覆盖、日志和通知状态。

## Unit Tests

- 诊断状态为 `ok`：采集运行、最近采集成功、评分核心无缺失且无过期。
- 诊断状态为 `warning`：Telegram 异常或全量研究覆盖不足，但评分核心可用。
- 诊断状态为 `error`：采集未运行、最近采集失败、评分核心缺失或过期。
- 日志文件不存在时不抛异常。
- 错误日志为空时 `last_error_line` 为 `None`。
- PID 文件存在但进程不存在时显示未运行。

## API Tests

- `/system/runtime` 保留现有字段。
- 新增诊断字段可 JSON 序列化。
- 如果新增 `/system/diagnostics`，应返回与 `/system/runtime` 一致的核心状态。

## Script Checks

- `scripts/status.ps1` 在服务不可达时给出启动命令。
- `scripts/status.ps1` 在采集未运行时给出 `start-system.ps1` 建议。
- `scripts/status.ps1` 在数据过期时显示缺失/过期槽位数量。

## UI Checks

- 面板状态区显示采集循环运行状态。
- 面板显示评分核心覆盖和全量研究覆盖。
- 面板出现错误时显示简短动作，不展示大段日志。

## Regression Commands

```powershell
python -m pytest -q
python -m compileall app tests
```

