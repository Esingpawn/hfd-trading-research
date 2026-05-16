# Darkflow Core Convergence Plan

## Decision

Do not rebuild the whole system from zero. Keep the infrastructure that is already useful, but converge the trading core around official darkflow playbooks and isolate older feature-candidate research as legacy/control evidence.

The target path is:

```text
raw_snapshot
-> signal normalization
-> DarkflowZone
-> DarkflowInteraction
-> TradeCandidate / DecisionCard
-> anti-repaint backtest
-> isolated shadow paper
-> real paper
-> live_small
```

Anything outside this path must be explicitly marked as `legacy_control`, `research_only`, or `infra_only` before it can appear in dashboard or reports.

## Why Not Rebuild

The earlier direction produced some noisy research paths, but the repo also contains assets that should not be thrown away:

- PostgreSQL, Alembic, Redis, task orchestration, and deployment structure.
- Raw payload storage and snapshot collection.
- Feature labeling and report materialization primitives.
- Shadow-paper infrastructure with cost/slippage modeling.
- Dashboard and API scaffolding.
- Official darkflow rulebook, playbooks, zones, and v2 interactions.
- Regression tests that prevent research code from opening paper/live orders.

The problem is not total architectural failure. The problem is that legacy research outputs can still look like primary trading evidence. The fix is boundary enforcement plus a new first-class trade-candidate layer.

## System Boundaries

### Core Darkflow Path

This path is allowed to drive future paper/live promotion after it passes gates:

- `app.services.darkflow_rules`
- `app.services.darkflow_playbooks`
- `app.services.darkflow_interactions`
- future `TradeCandidate` / `DecisionCard`
- future anti-repaint report
- future v2 shadow-paper promotion report

Current status: research-only. It must not open real paper or live orders.

### Legacy / Control Path

This path remains useful for comparison and data-quality analysis only:

- generic feature candidate screens
- generic segment candidate screens
- generic feature paper A/B reports
- `shadow_feature_candidates_v1`
- `baseline_v0` scoring evidence

These outputs must stay marked as not used for opening decisions, execution weights, or live trading. Dashboard should present them as legacy/control, not as the main trading result.

### Infrastructure Path

These should be preserved and improved:

- migrations and schema management
- raw payload references
- task runs and report cache patterns
- data quality checks
- paper and risk primitives
- storage maintenance

## Phase 1: Boundary Enforcement And Plan Lock

Goal: make the new/old boundary visible and testable.

Tasks:

- Add a shared research-lineage helper for `core_darkflow_v2`, `legacy_feature_research`, and `legacy_baseline_v0`.
- Add lineage metadata to darkflow v2 reports and policies.
- Add legacy metadata to feature-candidate reports and legacy shadow-paper reports.
- Update dashboard labels so generic feature/shadow results are visibly legacy/control.
- Add tests that assert legacy research cannot be mistaken for the core darkflow path.

Acceptance:

- Tests prove darkflow v2 is the only primary candidate research path.
- Tests prove legacy reports are still research-only and excluded from opening decisions.
- Dashboard text exposes `legacy/control` for old feature/shadow sections.

## Phase 2: TradeCandidate / DecisionCard Layer

Goal: convert darkflow interactions into complete candidate trades.

Tasks:

- Add `trade_candidates` table or equivalent model.
- Add decision-card generator from `DarkflowInteraction`.
- Store strategy, direction, setup time, entry plan, stop, TP levels, RR, rule score, quality score, supporting evidence, blockers, and lifecycle status.
- Add API endpoint for latest decision cards.
- Dashboard should show candidate cards before generic research tables.

Acceptance:

- Every candidate has entry, stop, target, RR, and blocker list.
- No candidate without stop or RR can be marked paper-eligible.
- Decision cards remain research-only until anti-repaint and shadow gates pass.

## Phase 3: Anti-Repaint Audit

Goal: prove whether darkflow historical evidence was visible at decision time.

Tasks:

- Persist payload hashes and normalized signal fingerprints for snapshots.
- Compare old saved snapshots against later reconstructed API responses.
- Report added/deleted/changed historical events by indicator, symbol, and timeframe.
- Add repaint-risk flags to candidate and backtest reports.

Acceptance:

- Each core indicator has a repaint-rate report.
- Backtests clearly state whether they are snapshot-safe or repaint-risk.
- Data quality can block promotion when repaint risk is too high.

## Phase 4: Strategy Backtest Hardening

Goal: turn current interaction replay into a defensible strategy backtest.

Tasks:

- Implement triple-barrier labels: TP, SL, and time exit.
- Evaluate in R multiples, not raw percent only.
- Add fee, slippage, stop penetration, and entry delay stress tests.
- Add time-based train/validation/test split and walk-forward reporting.
- Segment by strategy, symbol tier, timeframe, and market regime.

Acceptance:

- Research -> paper requires sample-out PF > 1.15, expectancy > 0.05R, at least 100 trades, and stress-test survival.
- Any report without walk-forward or stress evidence is blocked from promotion.

## Phase 5: V2 Shadow Paper Promotion

Goal: validate darkflow candidates in isolated forward-like paper before real paper.

Tasks:

- Separate v2 shadow stats from `shadow_feature_candidates_v1`.
- Track open/closed trades, expectancy, PF, max drawdown, and runner-exit behavior by playbook.
- Add promotion statuses for v2 candidates only.
- Ensure legacy shadow trades never count toward darkflow v2 promotion.

Acceptance:

- Darkflow v2 shadow report has its own strategy name and promotion gates.
- Real paper remains disabled until v2 shadow gates pass.

## Phase 6: Dashboard Convergence

Goal: make the product reflect the new core path.

Tasks:

- Command-center first screen: data health, mode, risk gate, darkflow v2 candidate cards.
- Move generic feature research and old shadow-paper to collapsed legacy/control sections.
- Show explicit blockers: missing anti-repaint report, insufficient shadow samples, weak PF, high drawdown.
- Show tutorial semantics for each decision card.

Acceptance:

- User can see whether the system is safe, what opportunity exists, and why it is blocked or allowed.
- Legacy tables cannot be confused with primary trading evidence.

## Phase 7: Cleanup And Deletion

Goal: remove dead weight only after the new path is stable.

Tasks:

- Move old modules behind legacy naming or delete unused entry points.
- Split oversized modules after behavior is covered by tests.
- Archive old reports that are not needed for controls.
- Keep migrations and historical data readable.

Acceptance:

- No production API uses legacy reports as primary trading evidence.
- Module boundaries are clear enough for future development.

## Current Work Item

Phase 1 is in place: darkflow v2 reports carry `core_darkflow_v2` lineage, and legacy feature/shadow research is explicitly marked as `Legacy/Control`.

Phase 2 is now active. The immediate implementation target is a persistent `trade_candidates` layer fed by `DarkflowInteraction v2 -> DecisionCard`. This turns temporary card output into lifecycle-tracked candidates that can later accumulate anti-repaint, shadow-paper, paper, and live promotion evidence without mixing with legacy research.
