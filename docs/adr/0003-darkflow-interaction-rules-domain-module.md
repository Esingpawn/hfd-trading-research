# ADR 0003: Darkflow Interaction Pure Rules Live In The Domain Module

## Status

Accepted

## Context

Darkflow Interaction generation is the core of the Core Darkflow v2 path. The service module also handles payload reading, database backfill, persistence, reports, and shadow replay, so small pure rules were hard to test without importing the whole service.

## Decision

Pure Darkflow Interaction rules live in `app/domain/darkflow/interactions.py`. The service module remains the database and report adapter.

## Consequences

- Kline normalization, playbook mapping, grade mapping, and stable key generation can be tested without database setup.
- The first version preserves existing behaviour and does not change zone extraction, interaction detection, backtest thresholds, or shadow replay rules.
- Future darkflow tutorial semantics should deepen this domain module before changing service adapters.
