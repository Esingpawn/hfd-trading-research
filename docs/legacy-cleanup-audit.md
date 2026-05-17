# 旧模块清理审计

这份审计把旧代码、兼容入口和生成文件按风险分层。目标是减少系统臃肿，但不破坏历史样本、研究基线、数据库迁移和当前生产 Dashboard 的兜底能力。

## 必须保留

下面这些不是删除对象。

- `migrations/versions/` 下的 Alembic 迁移：生产启动和历史数据库版本链路依赖它们。尤其是 `20260511_0007_snapshot_collection_runs.py`，它对应之前线上出现过的缺失 revision 问题。
- 核心研究表和模型：`feature_events`、`feature_labels`、`trade_candidates`、`paper_trades`、`shadow_paper_trades`、`experiment_runs` 以及相关 ORM 模型。它们保存历史证据、回测基线和影子交易样本。
- `app/services/research_lineage.py`：这里明确标记了 `core_darkflow_v2` 是当前主链路，旧 feature/shadow 输出只是对照证据。
- 暗流 v2 相关服务和测试：`darkflow_*`、交易卡、晋级闸门、防重绘审计、隔离 v2 影子纸上交易，是当前真正的策略主路径。

## 保留为兼容 / 对照

下面这些比较旧，但仍有兜底、测试或历史对照价值。

- `app/web/dashboard.html`：当 `web/dist/index.html` 存在时，它已经不是预期主 Dashboard；但 `/dashboard` 当前仍会在 React 构建不存在时回退到它。测试也还在校验这个兜底页暴露研究刷新、影子盘、血缘标签和暗流报告能力。
- `app/services/feature_candidates.py`：旧版通用特征研究对照链路。它不能直接驱动开仓决策，但仍可用于历史对照、回归检查和研究报告。
- `app/services/shadow_paper.py` 中的 `shadow_feature_candidates_v1`：旧版影子盘对照链路。它必须继续和 v2 晋级路径隔离，并在界面上标记为 `Legacy/Control`。
- `app/api/routers/signals.py` 里的 feature candidate 路由，以及 `app/application/tasks.py` 里的匹配任务：在 React Dashboard 和 v2 接口完全替代它们的用户可见职责前，先保留。

## 产品界面中隐藏或降级

下面这些不应该出现在首屏决策区域。

- 旧版候选特征报告。
- 旧版分段候选报告。
- 旧版/control 影子纸上交易统计。
- 没有转换成暗流 v2 `TradeCandidate` / `DecisionCard` 证据的通用实验报告。

界面规则：这些内容只能放在明确标记的 `Legacy/Control` 或研究归档区域，不能和主交易候选并列展示，避免误导你判断真实可观察机会。

## 可归档 / 生成产物

下面这些可以重新生成，不应作为源码提交。

- `web/dist/`：由 `npm run build` 生成，已经被 `.gitignore` 忽略。
- `web/node_modules/`：依赖安装目录，已经被 `.gitignore` 忽略。
- `web/tsconfig.tsbuildinfo`：TypeScript 增量构建产物，已经被 `.gitignore` 忽略。
- `hfd-deploy.zip` 这类本地部署压缩包：只是传输产物，现在已加入 `.gitignore`。

## 可以安全删除的本地杂物

这些可以在确认未被 Git 跟踪后从本地工作区删除。

- 仓库根目录下未跟踪的 `hfd-deploy.zip`。
- 磁盘空间紧张时，忽略目录里的旧本地构建产物。

不要把生产数据库行、迁移文件或历史实验记录纳入源码清理。如果数据库体积成为问题，需要单独制定保留、归档、备份和恢复演练方案。

## 删除 `app/web/dashboard.html` 前的退出条件

只有下面条件全部满足后，旧静态 Dashboard 才能移动到归档路径或删除。

- 本地和线上部署流程都能稳定生成 React `/dashboard`。
- React Dashboard 已覆盖交易卡、数据质量、防重绘状态、v2 影子纸上交易、纸上交易、回测实验室和风控安全状态。
- 旧版/control 报告仍可通过二级归档页面或 API 导出访问。
- `tests/test_dashboard_static.py` 已替换为 React 路由/构建测试和兼容 API 测试。

在这些条件满足前，保留旧文件，但把它视为兼容 UI，不再视为产品方向。
