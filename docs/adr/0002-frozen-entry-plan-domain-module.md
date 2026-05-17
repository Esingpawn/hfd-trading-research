# ADR 0002: Frozen Entry Plan Rules Live In The Domain Module

## Status

Accepted

## Context

Core Darkflow v2 candidates use a frozen entry plan so the intended entry, stop, target, validity window, and invalidation rules do not drift as market data changes. The rules for creating and evaluating these plans were split between decision-card generation and promotion/shadow-forward scanning.

## Decision

Frozen Entry Plan rules live in `app/domain/trade_candidates/entry_plan.py` as pure functions. Service modules may build database-backed adapters around these functions, but they do not own the plan state rules.

## Consequences

- A frozen entry plan has one shared interpretation across decision cards, shadow-forward scanning, and entry-plan reports.
- Services still read prices, candidates, and payloads, but the plan states `waiting`, `triggered`, `missed`, `expired`, `invalidated`, `invalid_shape`, and `missing_price` are calculated in the domain module.
- The first version preserves existing behaviour and does not change entry tolerance, validity windows, or promotion thresholds.
