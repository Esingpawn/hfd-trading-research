# Darkflow Indicator Usage Audit

This audit records how tutorial indicators currently affect the primary trading research path after the darkflow correction work.

## Primary Path

The primary path is:

```text
SignalSnapshot raw payload
-> DarkflowZone
-> DarkflowInteraction v2
-> DecisionCard
-> TradeCandidate
-> anti-repaint audit
-> isolated v2 shadow-forward
-> manual paper review
```

Only this path is allowed to become a future paper/live promotion candidate. Legacy feature reports and baseline scoring remain control evidence only.

## Implemented Usage

- `app/services/darkflow_rules.py` stores the official tutorial semantics and maps official indicator names to internal keys.
- `app/services/darkflow_playbooks.py` groups indicators into six research playbooks: cost pullback, liquidity sweep reversal, breakout confirmation, trend ride extension, exhaustion exit filter, and vacuum acceleration.
- `app/services/darkflow_interactions.py` converts raw snapshots and candles into darkflow zones, first-touch / wick-reclaim / body-break interactions, dynamic targets, quality scores, and backtest outcomes.
- `app/services/darkflow_decision_cards.py` turns v2 interactions into decision cards with fixed entry, stop, target, RR, quality evidence, blockers, and frozen entry plans.
- `app/services/darkflow_candidate_promotion.py` performs anti-repaint rebuild checks and opens only isolated v2 shadow-forward samples when the frozen entry plan is still triggered.

## Deeply Used In The Core Candidate Chain

These indicators are mapped into playbooks and can currently affect zone extraction, interaction quality, targets, confirmations, blockers, or trade-candidate generation when raw payload geometry is available:

- `smart_money_cost`
- `trend_price`
- `micro_poc`
- `hvn_nodes`
- `inst_volume_profile`
- `inst_vwap`
- `liq_heatmap`
- `liquidation_fuel`
- `liquidity_sweep`
- `retail_stop_loss`
- `cascade_liquidation_zones`
- `fair_value_gap`
- `liquidity_vacuum`
- `inst_choch`
- `trend_purity`
- `cross_exchange_resonance`
- `imbalance`
- `trend_exhaustion`

## Still Not Fully Modeled

These tutorial indicators are cataloged, but still need reliable collection and zone geometry parsing before they should influence real candidate entry or exit decisions:

- `max_pain`
- `trend_roi`
- `max_drawdown_tolerance`
- `time_exhaustion`
- `volume_exhaustion`
- `ob_decay`
- `poc_shift`
- `trailing_vwap`
- `trend_saturation`
- `absolute_zones`
- `fair_value`
- `power_imbalance`
- `time_heatmap`

## Current Boundary

- Generic `FeatureEvent + FeatureLabel` reports are still useful for coverage and quality diagnostics, but they are not the main strategy evidence.
- Legacy shadow trades under `shadow_feature_candidates_v1` must not count toward darkflow v2 promotion.
- `TradeCandidate.paper_eligible` and `TradeCandidate.live_eligible` remain false by default.
- Live trading remains blocked by the global trading safety state.

## Latest Correction

Decision cards now carry `frozen_darkflow_v2_entry_plan` data:

- fixed `frozen_at`
- fixed `valid_until`
- fixed `entry_range`
- fixed invalidation price
- explicit missed, expired, invalidated, waiting, and triggered states during shadow-forward evaluation

This directly addresses the previous dynamic-entry problem: an old candidate can now be observed as waiting, missed, expired, or invalidated instead of being reinterpreted as a fresh entry zone on every refresh.
