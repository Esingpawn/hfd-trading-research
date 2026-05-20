# Core Darkflow v2 正期望生产线 PRD

## Problem Statement

当前 HFD 已经完成了 Core Darkflow v2 的主路径、Trade Candidate、Frozen Entry Plan、Shadow Forward Sample、Promotion Gate、Dashboard、PostgreSQL/Redis/worker 等基础设施，但系统距离“长期可盈利系统”仍有关键缺口：它还没有把交易结果转化成足够严格、可复验、可分组的正期望筛选器。

从用户视角看，当前最痛的问题不是“没有更多指标”，而是：

- 纸上交易、影子交易的每笔结果不够直观，PnL、R 倍数、退出原因、止盈/止损/时间退场不够可信。
- 不同暗流剧本、币种、方向、周期、市场环境的结果混在一起，用户无法一眼知道哪些组合真的赚钱、哪些组合应该暂停。
- 很多候选可以产生，但不知道是否应该进入纸上复核；系统需要更会拒绝低质量机会。
- 时间退场、固定止盈、趋势持仓之间缺少可验证的反事实统计，导致 HYPE 这类趋势延展行情可能过早离场。
- 回测、影子、纸上之间还没有形成严格的分层晋级纪律，容易把历史拟合或低样本噪声误认为真实优势。

本 PRD 的核心问题是：如何把 HFD 从“暗流候选生成系统”迭代成“Core Darkflow v2 正期望生产线”，让系统只在统计上更可信的暗流场景里出手，并持续降低垃圾候选、伪优势和过拟合带来的损耗。

## Solution

建设一条围绕 Core Darkflow v2 的正期望闭环：

```text
Trade Candidate
-> Frozen Entry Plan
-> Shadow Forward Sample
-> Trade Outcome Truth
-> Setup Expectancy
-> Whitelist / Blacklist Evidence
-> Promotion Gate
-> Paper Review
-> Exit Optimization
-> Continuous Revalidation
```

系统不追求“每天都有单”，而是追求：

- 每一笔影子/纸上结果都能被准确复盘。
- 每个暗流剧本的胜率、盈利因子、平均 R、最大回撤、退出质量都能独立统计。
- 候选必须通过样本、期望、风险、反过拟合、市场环境过滤后才能进入更高阶段。
- 时间退场和趋势持仓由分组证据驱动，而不是全局拍脑袋放宽。
- Dashboard 能告诉用户：当前哪些剧本在赚钱、哪些剧本暂停、哪些候选为什么不能交易。

本 PRD 不承诺系统一定盈利。它的目标是把系统改造成一个更严谨的正期望发现、验证和执行框架，从而更接近长期可盈利交易系统。

## User Stories

1. As a trader, I want every paper trade to show real PnL, so that I can immediately know whether it made or lost money.
2. As a trader, I want every shadow forward sample to show real PnL, so that I can evaluate candidate quality before paper review.
3. As a trader, I want every trade to show R multiple, so that different symbols and price scales can be compared fairly.
4. As a trader, I want every closed trade to show exit reason, so that I can tell whether it hit stop, target, time exit, invalidation, or manual close.
5. As a trader, I want closed trades to show MFE and MAE, so that I can understand whether exits are too early or stops are too tight.
6. As a trader, I want time-exit trades to show what happened if held longer, so that the system can learn whether the time window is too short.
7. As a trader, I want exit reason labels to be Chinese and consistent, so that I can read trade history without decoding internal terms.
8. As a trader, I want each Trade Candidate to be tied to a darkflow setup type, so that I can understand the exact playbook behind a trade.
9. As a trader, I want setup types to match official tutorial semantics, so that the system does not drift back into generic feature scoring.
10. As a trader, I want cost-pullback setups separated from liquidity-sweep setups, so that their performance is not mixed together.
11. As a trader, I want trend-ride-extension setups separated from reversal setups, so that exits and targets can be evaluated differently.
12. As a trader, I want each setup to show sample count, win rate, profit factor, average R, and max drawdown, so that I can rank setups by actual evidence.
13. As a trader, I want setup performance segmented by symbol, direction, timeframe, and market state, so that a profitable long BTC setup does not falsely approve a weak short altcoin setup.
14. As a trader, I want the system to show whether a setup is collecting, review-ready, whitelisted, paused, or blacklisted, so that I can understand the next action.
15. As a trader, I want weak sub-portfolios to be automatically paused, so that they stop producing low-quality paper candidates.
16. As a trader, I want strong sub-portfolios to be whitelisted only after enough forward samples, so that paper review is based on real evidence.
17. As a trader, I want the whitelist to be based on Core Darkflow v2 evidence only, so that legacy/control research cannot accidentally promote trades.
18. As a trader, I want blacklisted combinations to show the exact reason, so that I know whether the issue is low win rate, low PF, high drawdown, or bad time exits.
19. As a trader, I want the Promotion Gate to include setup expectancy evidence, so that candidate eligibility reflects current forward performance.
20. As a trader, I want the Candidate Lifecycle to remain the source of truth for status, so that dashboard and worker logic cannot disagree.
21. As a trader, I want Dashboard to show “currently allowed setups”, so that I know what the system is permitted to trade.
22. As a trader, I want Dashboard to show “blocked but improving setups”, so that I can see where more samples are needed.
23. As a trader, I want Dashboard to show “do not trade” setups, so that I avoid low-quality opportunities.
24. As a trader, I want every decision card to explain the setup, entry plan, risk shape, blocker groups, and evidence summary, so that I can trust why the system says wait or reject.
25. As a trader, I want duplicate candidates to be deduped by frozen plan and setup context, so that one market opportunity does not inflate the sample count.
26. As a trader, I want the system to avoid opening shadow samples for expired or invalidated plans, so that stale opportunities do not contaminate statistics.
27. As a trader, I want anti-repaint evidence included before setup promotion, so that historical rewritten signals do not create fake confidence.
28. As a trader, I want setup statistics to distinguish backtest, shadow forward, and paper results, so that historical evidence is not treated as live-forward evidence.
29. As a trader, I want walk-forward or time-split validation for backtests, so that overfit historical results are not promoted.
30. As a trader, I want stress tests for fees, slippage, entry delay, and stop penetration, so that fragile setups are rejected.
31. As a trader, I want the system to show whether a setup’s edge is recent or stale, so that old performance does not keep approving current trades.
32. As a trader, I want edge decay rules, so that a once-good setup can be paused when recent forward samples deteriorate.
33. As a trader, I want time-exit extension to be per sub-portfolio, so that groups that benefit from holding longer can be treated differently from groups that deteriorate.
34. As a trader, I want any extended hold to use a protective trailing stop, so that the system never extends naked exposure.
35. As a trader, I want partial take-profit and runner logic tested in shadow first, so that trend extension does not damage baseline risk control.
36. As a trader, I want HYPE-like strong trend cases to be reviewed by MFE and runner evidence, so that early exits can be improved with data.
37. As a trader, I want weak trend-extension cases to keep shorter exits, so that a global exit change does not harm profitable short-hold setups.
38. As a trader, I want paper review to remain manual/HITL, so that no automated real paper or live promotion happens without human confirmation.
39. As a trader, I want live trading to stay disabled by default, so that research iteration cannot accidentally become real execution.
40. As a trader, I want all reports to be explainable in Chinese, so that I can operate the system quickly under pressure.
41. As a system operator, I want materialized expectancy reports, so that Dashboard can load quickly without scanning all raw trades each time.
42. As a system operator, I want heavy research jobs separated from live collector loops, so that production data collection remains fresh.
43. As a system operator, I want storage growth monitored by report, so that new truth-ledger and expectancy tables do not silently fill the disk.
44. As a developer, I want trade outcome calculation in a deep pure module, so that PnL/R/MFE/MAE can be tested without a database.
45. As a developer, I want setup expectancy calculation in a deep pure module, so that whitelist and blacklist thresholds can be tested deterministically.
46. As a developer, I want exit optimization decisions in a pure policy module, so that extension decisions do not depend on dashboard code.
47. As a developer, I want Promotion Gate to remain read-only, so that it aggregates evidence without mutating Trade Candidate state.
48. As a developer, I want Candidate Lifecycle to consume normalized evidence only, so that persistence and decision rules stay separated.
49. As a developer, I want Legacy/Control Research excluded from opening decisions by tests, so that old research paths remain safely isolated.
50. As a developer, I want API contracts for outcome, expectancy, whitelist, blacklist, and exit-review reports, so that the React dashboard can consume stable data.
51. As a developer, I want migration scripts for any new persisted result tables, so that production and local databases stay aligned.
52. As a developer, I want backfill scripts to reconstruct historical trade outcomes where possible, so that existing samples become useful without manual work.
53. As a developer, I want missing data to be explicit in reports, so that incomplete PnL or MFE/MAE does not masquerade as zero.
54. As a developer, I want quality gates to treat missing PnL as invalid, so that `0%` placeholders cannot improve statistics.
55. As a developer, I want tests proving no paper/live orders are opened by research reports, so that safety constraints remain intact.

## Implementation Decisions

- Build a **Trade Outcome Truth** module as the canonical calculator for paper and shadow outcomes. It should calculate net PnL, gross PnL, R multiple, MFE, MAE, exit reason normalization, fee/slippage impact, and missing-data flags from stable inputs.
- Persist or materialize a trade outcome truth record for each closed paper trade and each closed Core Darkflow v2 Shadow Forward Sample. This record should never overwrite raw trade data; it is a normalized read model for evaluation.
- Missing outcome values must be represented as missing/invalid, not as `0%`. Any setup report that includes invalid outcome rows must show the invalid count and exclude them from promotion statistics.
- Add a **Darkflow Setup Taxonomy** that maps Trade Candidates to official tutorial playbooks and setup types such as cost pullback, liquidity sweep reversal, breakout confirmation, trend ride extension, exhaustion exit filter, and vacuum acceleration.
- Each setup identity should include at minimum strategy family, setup type, symbol, direction, timeframe, and market state. More granular keys can be added later, but these dimensions are the minimum required for whitelist/blacklist decisions.
- Add a **Setup Expectancy Engine** as a deep module. It should aggregate closed outcomes into sample count, win rate, average R, median R, profit factor, max drawdown, time-exit share, average MFE, average MAE, and invalid-outcome count.
- Setup expectancy must separate evidence source: backtest, shadow forward, paper, and legacy/control. Only Core Darkflow v2 shadow-forward and paper evidence may influence future paper review gates.
- Add a **Whitelist / Blacklist Evidence Policy** as a pure decision module. It should classify sub-portfolios as collecting, review-ready, whitelist, pause, or blacklist based on configurable thresholds.
- Candidate Lifecycle remains the only owner of Trade Candidate status and promotion blockers. The whitelist/blacklist policy provides normalized evidence; it does not directly mark candidates eligible.
- Promotion Gate remains read-only. It should display setup expectancy evidence, whitelist/blacklist state, grouped blockers, and next action without mutating candidate state.
- Add a **Time Exit & Runner Review** module that evaluates post-time-exit counterfactual windows and recommends keep-time-exit, collect-more, or extend-with-trailing-stop per sub-portfolio.
- Time-exit extension must be segmented by setup identity. A global “hold longer” switch is explicitly rejected.
- Trend runner logic must first run in Shadow Forward Sample mode. It may add simulated partial take-profit, runner, and trailing-stop outcomes, but it must not change real paper execution until separately approved.
- Backtest evidence must be hardened before promotion: include R-multiple evaluation, fee/slippage/entry-delay/stop-penetration stress tests, sample-out or walk-forward splits, and anti-repaint risk labels.
- Dashboard should add a “正期望控制台” view that answers three questions: what is allowed, what is collecting evidence, and what is paused/blacklisted.
- Dashboard trade cards should include outcome truth fields and explain exit reasons in Chinese.
- Dashboard setup reports should show evidence source and promotion boundary clearly. Legacy/Control Research must remain visually separated.
- Heavy expectancy and backtest reports should be materialized periodically so dashboard reads fast cached results rather than scanning all raw rows.
- Existing safety policy remains unchanged: live trading disabled, manual confirmation required, and real paper handoff remains HITL.
- This PRD intentionally deepens existing Core Darkflow v2 modules rather than rebuilding the system from scratch.

## Testing Decisions

- Tests should verify external behavior and decision outputs, not private implementation details.
- Trade Outcome Truth tests should cover long and short trades, stop exits, target exits, time exits, manual exits, fee/slippage, missing exit prices, zero placeholders, MFE, MAE, and R multiple calculation.
- Setup Expectancy Engine tests should cover grouped aggregation, invalid-row exclusion, profit factor calculation, max drawdown, time-exit share, and source separation.
- Whitelist / Blacklist Evidence Policy tests should cover insufficient samples, positive expectancy, weak PF, high drawdown, recent deterioration, invalid data, and blacklisting.
- Time Exit & Runner Review tests should cover post-exit windows, improved share, average delta, harmful extension groups, and the requirement that extension uses protective trailing stops.
- Candidate Lifecycle tests should be extended only through normalized evidence inputs. They should prove lifecycle remains the final owner of promotion status.
- Promotion Gate tests should prove expectancy evidence appears in blocker groups and next-action text without mutating candidate state.
- API tests should verify outcome, expectancy, whitelist/blacklist, and time-exit report contracts.
- Dashboard/static tests should verify Chinese labels, empty states, and that missing PnL is not displayed as `0%`.
- Regression tests should prove Legacy/Control Research cannot mark a candidate paper eligible or count toward Core Darkflow v2 promotion.
- Safety tests should prove no research, expectancy, dashboard, or shadow-forward code can open live orders.
- Prior art in this repo includes tests for Trade Candidate Lifecycle, Frozen Entry Plan, Promotion Gate, Shadow Paper, Paper Stats, Darkflow Interactions, Darkflow Playbooks, and Research Lineage. New tests should follow those boundaries.

## Out of Scope

- Enabling live trading.
- Automatically promoting candidates into real paper trading without human review.
- Guaranteeing profit or claiming the system is already a profitable trading machine.
- Rebuilding the entire HFD system from scratch.
- Replacing PostgreSQL/Redis/Docker infrastructure.
- Adding unrelated new indicators before the existing tutorial indicators are measured by setup expectancy.
- Using Legacy/Control Research as primary opening evidence.
- Global loosening of time exits without sub-portfolio evidence.
- Building exchange execution integrations for real-money orders.

## Further Notes

- This PRD aligns with the existing Core Darkflow v2 convergence plan and ADRs:
  - Candidate Lifecycle remains the single source of truth for candidate state.
  - Frozen Entry Plan remains fixed and must not drift with later market data.
  - Promotion Gate remains read-only.
  - Legacy/Control Research remains comparison evidence, not opening evidence.
- The product goal is to make the system increasingly selective. A lower number of higher-quality candidates is a better outcome than frequent low-quality signals.
- “胜率” should not be optimized alone. The target metric is positive expectancy after fees, slippage, execution errors, and drawdown constraints.
- The highest-priority implementation order should be:
  1. Trade Outcome Truth.
  2. Setup Expectancy Engine.
  3. Whitelist / Blacklist Evidence Policy.
  4. Promotion Gate and Dashboard integration.
  5. Time Exit & Runner Review.
  6. Backtest hardening and continuous revalidation.
