# Darkflow Official Tutorial Rulebook

This document records the correction from `baseline_v0` scoring to tutorial-driven darkflow research.

Source reviewed: `dash.hfd.fund/pro` tutorial button content embedded in `indicatorConfigs`.

## Policy

- Old cost-band and feature-candidate reports remain useful as infrastructure and control evidence only.
- Official tutorial indicators must first enter a playbook before they affect scoring or opening decisions.
- The current v1 implementation is research-only. It does not open live orders, paper trades, or change strategy weights.
- A playbook can only be promoted after zone-interaction backtests and isolated shadow-paper validation.

## Implemented v1 Loop

- `app/services/darkflow_rules.py` stores the official rulebook and internal indicator mapping.
- `app/services/darkflow_playbooks.py` groups labeled feature events into tutorial playbooks.
- `/darkflow/rulebook` exposes the full official mapping.
- `/darkflow/playbooks/backtest` runs a tutorial-semantics proxy backtest.
- `darkflow.playbook_backtest` and `python -m app.cli darkflow-playbook-backtest --persist` materialize reports into `experiment_runs`.
- Dashboard shows the latest materialized playbook report in the research section.

## Playbooks

1. `pullback_to_cost`: smart-money cost, trend pressure, Micro POC, HVN, volume profile, institutional VWAP.
2. `liquidity_sweep_reversal`: liquidity sweep, liquidation heatmap, retail stop loss, cascade liquidation zones.
3. `breakout_confirmation`: CHoCH, whale action, imbalance, power imbalance, order-wall decay.
4. `trend_ride_extension`: trend-strength and darkflow target evidence used to reduce premature take-profit exits.
5. `exhaustion_exit_filter`: trend/time/volume/ROI/drawdown exhaustion used as exit or no-chase filters.
6. `vacuum_acceleration`: FVG, liquidity vacuum, volume profile and trend purity used for one-way acceleration tests.

## Known Gap Before Real Paper Integration

The v1 backtest uses existing standardized `FeatureEvent + FeatureLabel` rows. It does not yet model raw zone interactions:

- first touch
- wick pierce and reclaim
- body break invalidation
- zone decay after repeated tests
- death-line and time-exhaustion geometry
- heatmap/fuel target distance

The next correction step is to create explicit `DarkFlowZone` and `DarkFlowInteraction` records from raw payload plus candles, then rerun playbook backtests on those interaction events before enabling any paper-scan opening logic.
