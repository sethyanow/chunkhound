---
id: ch-ntc
title: Debug DuckDB path escaping for file scopes
status: closed
type: task
priority: 1
owner: Seth
---





## Requirements

## Context

## Success Criteria

## Log

- [2026-04-04T15:04:47Z] [Seth] Added debug instrumentation in shared scope filter, graph/search symbol query builders, and DuckDB execute path to capture ESCAPE literals and query params for .ts path failures. Ready for runtime reproduction.
- [2026-04-04T18:20:51Z] [Seth] Starting fix — changing LIKE escape character from backslash to '!' to avoid sqlglot mangling. Regression test first.
