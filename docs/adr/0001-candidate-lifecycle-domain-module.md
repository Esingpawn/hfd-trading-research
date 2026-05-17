# ADR 0001: Candidate Lifecycle Decisions Live In The Domain Module

## Status

Accepted

## Context

HFD's Core Darkflow v2 path promotes opportunities through `TradeCandidate` records. Candidate state was being derived in multiple service modules, which made it hard to prove why a candidate was blocked, collecting shadow samples, retired, or ready for paper review.

## Decision

`Candidate Lifecycle` is the single source of truth for calculating `TradeCandidate.status`, `promotion_status`, and `promotion_blockers` from normalized candidate evidence.

The first implementation lives in `app/domain/trade_candidates/lifecycle.py` as a pure decision module. It does not query the database, create shadow trades, call external APIs, or commit transactions.

## Consequences

- Service modules collect evidence and persist lifecycle decisions, but do not own the final lifecycle rules.
- Dashboard modules display lifecycle output, but do not recalculate candidate eligibility.
- Shadow-forward modules provide sample evidence, but do not decide paper or live eligibility.
- The first version preserves existing behaviour and does not change the database schema.
