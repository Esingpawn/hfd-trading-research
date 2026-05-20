# HFD 有效性收敛审计

## 结论

当前系统不应该继续堆叠新指标、新页面或新实验入口。下一阶段只保留一条能证明正期望的主线：

```text
Core Darkflow v2
-> Trade Candidate
-> Frozen Entry Plan
-> Shadow Forward Sample
-> Trade Outcome Truth
-> Setup Expectancy
-> Whitelist / Blacklist Evidence
-> Promotion Gate
-> Paper Review
```

任何不能改善这条链路的功能，都只能作为归档、对照或运维工具存在。

## 保留为主路径

这些模块直接影响是否更接近长期可盈利系统：

- `app/services/darkflow_rules.py`
- `app/services/darkflow_playbooks.py`
- `app/services/darkflow_interactions.py`
- `app/services/darkflow_decision_cards.py`
- `app/services/darkflow_candidate_promotion.py`
- `app/services/darkflow_alpha.py`
- `app/domain/trade_candidates/*`
- `app/domain/trade_outcomes.py`
- `app/domain/setup_expectancy.py`
- `app/domain/whitelist_blacklist_policy.py`
- `app/domain/shadow_forward_samples.py`
- `app/services/shadow_paper.py` 中的 `darkflow_v2_trade_candidate_shadow_forward_v1`
- Dashboard 中的交易卡片、候选池、入场计划、影子交易、纸上交易、回测中心、指标教程映射

主路径的验收指标只有这些：

- 是否提高 Core Darkflow v2 的有效样本质量
- 是否提高 setup 级 Profit Factor、平均 R、胜率下界
- 是否降低最大回撤、无效结果、重复样本、时间退场损耗
- 是否让 Promotion Gate 更会拒绝差机会
- 是否让退出逻辑更少错过趋势延伸，同时不扩大裸露风险

## 降级为研究归档

这些模块不再允许被误读为开仓证据：

- `app/services/feature_candidates.py`
- `FeatureEvent + FeatureLabel` 通用候选筛选
- 旧 feature / segment candidate 报告
- 旧 feature / segment paper A/B
- `shadow_feature_candidates_v1`
- Dashboard 的旧研究对照页

它们只允许用于：

- 历史对照
- 数据质量诊断
- 回归检查
- 将某个旧发现重新翻译成官方暗流教程语义后，再进入 Core Darkflow v2

它们不能直接：

- 改变开仓权重
- 标记 `TradeCandidate.paper_eligible`
- 计入 Core Darkflow v2 setup 白名单
- 作为 Promotion Gate 的正向证据

## 生产任务闸门

为了防止无用堆叠，`/tasks/enqueue` 默认只允许 `production_allowed=true` 的任务排队。

Legacy/Control 或未知任务必须显式传 `force=true` 才能排队。这样保留维护能力，但避免旧研究任务被误触发后污染主线判断。

允许默认排队的任务类型：

- 采集、价格、数据质量、存储维护
- Core Darkflow v2 候选、审计、影子前向、Promotion Gate、Alpha 记分牌、等待入场刷新

默认阻止的任务类型：

- 通用 feature backfill / label / refresh
- 旧候选筛选和旧 A/B
- 旧 shadow feature scan / replay
- 未登记任务

## 删除或停用标准

一个模块如果连续满足以下条件，可以进入删除候选：

- 不是 Core Darkflow v2 主路径
- 不被 Dashboard 主视图使用
- 不被生产 Docker loop 调用
- 不保存必须保留的历史样本
- 没有独立回归价值
- 删除后不会让正期望闭环缺少证据

删除前必须有测试证明：

- Legacy/Control 仍不能进入 Promotion Gate
- Paper/Live 仍不会被研究任务打开
- Dashboard 对旧数据的缺失有明确空状态

## 下一步唯一优先级

不要继续加新指标。下一步只做：

1. setup 级结果审计：按 `strategy_id + setup_type + symbol + direction + timeframe + market_state` 找出真正正期望组合。
2. 时间退场和趋势 runner 的分组优化：只对证据支持的子组合延长，不做全局放宽。
3. Promotion Gate 收紧：低 PF、高回撤、高无效结果、时间退场损耗严重的组合必须阻断。
4. Dashboard 首屏只回答三件事：现在有什么机会、为什么能/不能交易、这个 setup 的前向证据是否为正。

这份审计的目的不是让系统变小，而是让系统停止假装所有研究都同等重要。
