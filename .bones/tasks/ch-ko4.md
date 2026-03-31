---
id: ch-ko4
title: populate_files crashes entire loop on single file failure
status: open
type: bug
priority: 0
parent: ch-0um
---





## Context
Surfaced during ch-yda acceptance demo. `populate_files()` iterates all files
but has no per-file error handling. A single `LSPTransportError` (e.g., client
enters DEGRADED state mid-loop) crashes the loop, skipping remaining files AND
the `_populate_workspace_symbols()` pass at the end.

Observed: indexing 740 files, population ran for ~639 files, then client
degraded and the remaining files + workspace pass were skipped. 34,600 symbols
populated but cross-file edges nearly absent (1,319/1,323 self-referential).

## Requirements
1. `populate_files` must catch per-file failures and continue to the next file
2. `_populate_workspace_symbols` must run even if some files failed
3. Failed files should be logged with structured reason (not silently dropped)
4. Summary at end: X files populated, Y failed, Z skipped (no LSP server)

## Success Criteria
- [ ] Single degraded client does not crash the population loop
- [ ] `_populate_workspace_symbols` runs after the file loop regardless of per-file failures
- [ ] Failed files are logged with file path and error detail
- [ ] `uv run scripts/demo_lsp.py` cross-file edge health check passes after re-index
