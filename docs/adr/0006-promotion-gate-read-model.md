# ADR 0006: Promotion Gate Is A Read-Only Report Module

## Status

Accepted

## Context

Core Darkflow v2 needs a single user-facing conclusion before any real paper handoff. Candidate Lifecycle owns status decisions, while Anti-Repaint Evidence, Frozen Entry Plan state, Shadow Forward Samples, duplicate exposure, and market quality are separate evidence sources.

## Decision

Promotion Gate is a read-only report module. It aggregates Core Darkflow v2 evidence into a product-level `gate_status`, grouped blockers, evidence summary, and next action. It does not mutate Trade Candidate state, open Shadow Forward Samples, or mark paper/live eligibility.

The first version stops at `review_ready`; it does not emit `paper_ready_candidate`.

## Consequences

- Candidate Lifecycle remains the only owner of lifecycle state.
- Legacy/Control Research is excluded from Promotion Gate.
- Dashboard and paper handoff can consume one stable conclusion without duplicating blocker grouping logic.
- Real paper entry remains HITL and out of scope for this report module.
