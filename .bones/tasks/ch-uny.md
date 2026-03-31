---
id: ch-uny
title: LSP client degrades during batch population — investigate root cause
status: open
type: bug
priority: 1
depends_on: [ch-ko4]
---



## Context
Surfaced during ch-yda acceptance demo. After populating ~639/740 files
(~15 minutes), the pyright LSP client enters DEGRADED state. Error:
`Client not ready (state: degraded)`. The pool should respawn degraded
clients, but something about long-running batch usage triggers degradation.

Possible causes:
- Pyright process exits after timeout/memory pressure during 700+ file processing
- `_monitor_process` detects unexpected exit and sets state to DEGRADED
- Transport pipe breaks during a long-running session

Depends on ch-ko4 (error handling) — with per-file resilience, the pool
can respawn after degradation and continue. But we should still understand
WHY it degrades to prevent it if possible.

## Requirements
1. Investigate: reproduce the degradation and capture the pyright exit code/reason
2. If pyright crashes: determine if it's memory, timeout, or protocol issue
3. If fixable: implement the fix (e.g., periodic client restart, memory limits)
4. If inherent: document as known limitation and ensure pool respawn works

## Success Criteria
- [ ] Root cause identified and documented in this skeleton
- [ ] Either fixed or documented with mitigation (pool respawn on degradation)
- [ ] `uv run chunkhound index .` completes population without degradation warning
- [ ] OR population completes with respawn (ch-ko4 handles recovery)
