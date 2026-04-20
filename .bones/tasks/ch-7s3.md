---
id: ch-7s3
title: 'Bug: _deserialize_metadata fails on pandas NaN (''JSON object must be str, bytes or bytearray, not float'')'
status: open
type: bug
priority: 2
parent: ch-ljh
---


## Context
Daemon log: `Error getting chunks with metadata: the JSON object must be str, bytes or bytearray, not float`. Occurs when pandas represents a chunk's missing metadata cell as `float('nan')`, and `_deserialize_metadata` passes NaN into `json.loads()` which raises TypeError. The error is silently logged and the entire chunks-with-metadata call returns `[]`.

**Reproduction:**
1. Any LanceDB chunks table where at least one chunk has null/missing metadata
2. Call any code path through `_executor_get_all_chunks_with_metadata` (indexing coordinator uses this)
3. Observe error log entry and empty return

## Diagnosis
Root cause: `chunkhound/providers/database/lancedb_provider.py:173-175`:
```python
def _deserialize_metadata(metadata_json: str | None) -> dict:
    """Deserialize chunk metadata from JSON string."""
    return json.loads(metadata_json) if metadata_json else {}
```

Behavior:
- `bool(float('nan'))` is `True` in Python — NaN is truthy
- Falls into `json.loads(float('nan'))` → `TypeError: the JSON object must be str, bytes or bytearray, not float`
- Pandas represents missing JSON string cells as NaN floats when the column is nullable

Fix location: `chunkhound/providers/database/lancedb_provider.py:_deserialize_metadata:173-175`.

## Implementation

### Step 1: Write failing tests
File: `tests/unit/providers/test_lancedb_deserialize_metadata.py` (new)
- `test_nan_returns_empty_dict` — `_deserialize_metadata(float('nan'))` returns `{}`
- `test_none_returns_empty_dict` — `_deserialize_metadata(None)` returns `{}`
- `test_empty_string_returns_empty_dict` — `_deserialize_metadata('')` returns `{}`
- `test_valid_json_returns_dict` — `_deserialize_metadata('{"a": 1, "b": [1,2]}')` returns `{"a": 1, "b": [1,2]}`
- `test_invalid_json_raises` — `_deserialize_metadata('not json')` raises `json.JSONDecodeError` (hiding real corruption would be wrong)

### Step 2: Run tests
```
uv run pytest tests/unit/providers/test_lancedb_deserialize_metadata.py -v
```
Expect failure on NaN case.

### Step 3: Fix
File: `chunkhound/providers/database/lancedb_provider.py:173-175`
```python
def _deserialize_metadata(metadata_json: str | None) -> dict:
    """Deserialize chunk metadata from JSON string. Returns {} for None, NaN, or empty."""
    if not isinstance(metadata_json, str) or not metadata_json:
        return {}
    return json.loads(metadata_json)
```
The `isinstance(str)` check covers None, NaN, and any non-string input defensively.

### Step 4: Run tests
Expect pass.

### Step 5: Audit other deserialization sites
Use LSP:
- `LSP.findReferences` on `_deserialize_metadata` — confirm all call sites rely on the new safe behavior
- Check if any caller has its own defensive NaN handling that becomes redundant (remove if so)

### Step 6: Run targeted suite
```
uv run pytest tests/unit/providers/ tests/integration/test_lancedb_*.py -v
```

### Step 7: Commit
```
git add -u && git commit -m "fix(lancedb): _deserialize_metadata handles pandas NaN without raising"
```

## Success Criteria
- [ ] NaN metadata returns `{}` — no TypeError
- [ ] All existing happy-path cases still work (valid JSON → dict)
- [ ] Invalid JSON still raises (don't hide real corruption)
- [ ] Unit tests pass; integration tests still pass
- [ ] Live verification: run `_executor_get_all_chunks_with_metadata` on this repo's DB without the error log

## Anti-Patterns
- NO `if metadata_json:` truthy check — NaN is truthy, defeats the purpose
- NO `except json.JSONDecodeError: return {}` — would hide real corruption
- NO `str(metadata_json)` coercion — `str(nan)` is `'nan'` which isn't valid JSON either

## Key Considerations
- Related to ch-qxc (semantic search timeout). If the formatting loop in `_executor_search_semantic` hits a NaN metadata row, `_deserialize_metadata` raises, the outer `except Exception` catches and re-raises as RuntimeError. Search fails. This fix removes that failure mode; verify during ch-qxc instrumentation whether this was a contributor.
- pandas NaN vs numpy NaN vs None — all non-string. `isinstance(str)` covers them uniformly.
- Future: consider adding a type annotation `metadata_json: str | None | float` to make the pandas-interaction explicit, or change the column read to materialize None instead of NaN. Out of scope for this bug.

## Log

- [2026-04-20T18:40:11Z] [Seth] Diagnosis HIGH confidence: _deserialize_metadata uses truthy check on metadata_json; pandas NaN is truthy; json.loads(nan) raises TypeError. Fix: isinstance(str) check.
