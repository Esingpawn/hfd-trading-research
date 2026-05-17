# ADR 0004: Legacy/Control Research Has A Shared Registry

## Status

Accepted

## Context

HFD keeps older feature and baseline research for comparison and data-quality evidence, but Core Darkflow v2 is the primary candidate path. Legacy reports and shadow strategies need a shared lineage registry so they cannot be confused with opening evidence.

## Decision

Research lineage constants and payloads live in `app/domain/research_lineage/registry.py`. Service-level helpers delegate to this registry for compatibility.

## Consequences

- Legacy/Control Research has one shared payload shape across reports, shadow stats, tasks, and future dashboard adapters.
- Legacy feature research remains available as control evidence, but does not become a Trade Candidate path.
- The first version preserves existing behaviour and does not delete old research modules or data.
