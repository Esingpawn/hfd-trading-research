# HFD Trading Research Context

This context records the project language for HFD's darkflow-first trading research system so architecture reviews and AI-assisted changes preserve the intended strategy path.

## Language

**Core Darkflow v2**:
The primary research and future paper/live promotion path built from official darkflow tutorial semantics.
_Avoid_: baseline scoring, generic feature research, legacy candidate research

**Legacy/Control Research**:
Historical feature, segment, and baseline research kept only for comparison, regression checks, and data-quality evidence.
_Avoid_: primary signal path, opening evidence

**Darkflow Interaction**:
A price reaction to a tutorial-defined darkflow zone, such as first touch, reclaim, invalidation, or target hit.
_Avoid_: generic signal event

**Trade Candidate**:
A lifecycle-tracked darkflow opportunity with frozen entry, stop, target, risk/reward, evidence, blockers, and promotion state.
_Avoid_: raw signal, untracked alert

**Decision Card**:
A read model that explains a **Trade Candidate** or candidate-like opportunity in user-facing trading language.
_Avoid_: dashboard row, generic report item

**Candidate Lifecycle**:
The single source of truth for converting candidate evidence into `status`, `promotion_status`, and `promotion_blockers`.
_Avoid_: scattered promotion logic, dashboard status logic

**Frozen Entry Plan**:
A time-limited entry, stop, target, invalidation, and entry-range plan captured when the **Trade Candidate** is created.
_Avoid_: dynamic entry price, moving open price

**Anti-Repaint Evidence**:
Evidence that the source darkflow signals were visible at decision time and did not depend on later-rewritten history.
_Avoid_: backtest confidence by itself

**Shadow Forward Sample**:
An isolated paper-like trade opened from a qualified **Trade Candidate** only to measure forward behaviour without affecting real paper or live trading.
_Avoid_: real paper trade, live trade

**Promotion Gate**:
A read-only conclusion layer that aggregates candidate evidence into grouped blockers, a `gate_status`, and the next action before real paper handoff.
_Avoid_: lifecycle state owner, paper execution switch

## Relationships

- **Core Darkflow v2** produces **Darkflow Interactions**.
- A **Darkflow Interaction** can become a **Trade Candidate**.
- A **Trade Candidate** has exactly one **Frozen Entry Plan** at creation time.
- A **Trade Candidate** may accumulate **Anti-Repaint Evidence** and **Shadow Forward Samples**.
- The **Candidate Lifecycle** is the only place that converts candidate evidence into final promotion state and blockers.
- The **Promotion Gate** reads **Candidate Lifecycle**, **Frozen Entry Plan**, **Anti-Repaint Evidence**, and **Shadow Forward Samples** to produce a review conclusion without mutating candidate state.
- **Decision Cards** expose **Trade Candidates** to the dashboard without owning lifecycle decisions.
- **Legacy/Control Research** cannot promote a candidate unless it is converted into the **Core Darkflow v2** path.

## Example dialogue

> **Dev:** "Can the shadow-paper module mark this candidate as paper eligible?"
> **Domain expert:** "No. Shadow paper only provides **Shadow Forward Samples**. The **Candidate Lifecycle** decides whether the **Trade Candidate** is eligible."

## Flagged ambiguities

- "候选" has been used for both generic feature candidates and darkflow trade opportunities. Resolved: use **Trade Candidate** only for **Core Darkflow v2** opportunities; use **Legacy/Control Research** for old feature and segment candidates.
- "纸上交易" has been used for both real paper tracking and isolated forward validation. Resolved: use **Shadow Forward Sample** for isolated candidate validation and reserve paper trading language for real paper workflows.
