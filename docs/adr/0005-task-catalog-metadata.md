# ADR 0005: Task Metadata Lives In A Task Catalog

## Status

Accepted

## Context

Task execution is currently implemented as a large dispatcher. Core Darkflow v2, Legacy/Control Research, and infrastructure tasks were mixed in the same entry points, making it hard to tell which jobs are appropriate for production by looking at the task name alone.

## Decision

Task metadata lives in `app/application/task_catalog.py`. The first version records canonical names, aliases, research lineage, production allowance, and heavy-task status.

## Consequences

- Existing task execution behaviour is preserved.
- Task results include catalog metadata when the task name is known.
- Future CLI, API, worker, and dashboard adapters can use the same catalog instead of duplicating task classification.
