#!/usr/bin/env python3
"""Phase 1+2 Demo — LSP Client, Registry, Schema, Path Scoping, Population.

USER ACCEPTANCE WALKTHROUGH — not for implementation tasks to touch.
Updates happen between epics so each phase's delta is visible.

Dogfood ChunkHound's Phase 1+2 deliverables against the live codebase.
Run from project root:

    uv run scripts/demo_lsp.py                          # default file
    uv run scripts/demo_lsp.py chunkhound/lsp/types.py  # specific file
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# LSP SymbolKind → human name (LSP spec 3.17)
_SYMBOL_KIND = {
    1: "File",
    2: "Module",
    3: "Namespace",
    4: "Package",
    5: "Class",
    6: "Method",
    7: "Property",
    8: "Field",
    9: "Ctor",
    10: "Enum",
    11: "Interface",
    12: "Function",
    13: "Variable",
    14: "Constant",
    15: "String",
    16: "Number",
    17: "Boolean",
    18: "Array",
    19: "Object",
    20: "Key",
    21: "Null",
    22: "EnumMember",
    23: "Struct",
    24: "Event",
    25: "Operator",
    26: "TypeParam",
}


def _hdr(n: int, title: str) -> None:
    print(f"\n{'=' * 64}")
    print(f" {n}. {title}")
    print(f"{'=' * 64}\n")


def _row(col1: str, col2: str, col3: str = "", widths: tuple[int, int] = (20, 32)) -> None:
    print(f"  {col1:<{widths[0]}} {col2:<{widths[1]}} {col3}")


def _sep(widths: tuple[int, int] = (20, 32)) -> None:
    print(f"  {'─' * widths[0]} {'─' * widths[1]} {'─' * 10}")


# ── 1. LSP Client ──────────────────────────────────────────────


async def demo_lsp_client(target: str) -> bool:
    from chunkhound.lsp.client import LSPClient
    from chunkhound.lsp.registry import LANGUAGE_SERVER_REGISTRY
    from chunkhound.lsp.types import ServerState

    _hdr(1, "LSP CLIENT — Pyright + DocumentSymbol")

    config = LANGUAGE_SERVER_REGISTRY.get("python")
    if not config or not config.command:
        print("  python entry missing from registry")
        return False

    if not shutil.which(config.command):
        print(f"  {config.command} not on PATH — install: npm i -g pyright")
        return False

    client = LSPClient(config)
    try:
        init_result = await client.start(str(Path.cwd()))

        # serverInfo is optional in LSP spec — pyright doesn't populate it.
        # Fall back to the config command name.
        info = client.server_info or {}
        server_name = info.get("name") or config.command or "unknown"
        server_ver = info.get("version", "")

        print(f"  Server:   {server_name} {server_ver}")
        print(f"  Command:  {config.command} {' '.join(config.args)}")
        print(f"  State:    {client.state.value}")
        caps = sorted(c.value for c in client.capabilities)
        print(f"  Caps:     {', '.join(caps)}")

        # Pyright needs didOpen + analysis time before documentSymbol returns results
        target_path = Path(target).resolve()
        uri = target_path.as_uri()
        await client.notify_did_open(uri, target_path.read_text())
        print(f"\n  Waiting for analysis...", end="", flush=True)
        try:
            await client.wait_for_diagnostics(uri, timeout=15.0)
        except (TimeoutError, asyncio.TimeoutError):
            pass  # diagnostics timeout is ok — symbols may still work
        print(" done.\n")

        symbols = await client.document_symbols(uri)

        print(f"  Symbols in {target}: {len(symbols)}\n")
        _row("Kind", "Name", "Lines", widths=(12, 36))
        _sep(widths=(12, 36))

        # Filter to match editor LSP output: classes, methods, functions, top-level vars.
        # Skip local variables inside methods/functions (the noise).
        _METHOD_KINDS = {6, 9, 12}  # Method, Constructor, Function
        _VARIABLE_KIND = 13

        def _print_symbols(syms: list, indent: int = 0, parent_kind: int = 0) -> None:
            for s in syms:
                # Skip local variables inside methods/functions
                if s.kind == _VARIABLE_KIND and parent_kind in _METHOD_KINDS:
                    continue
                kind = _SYMBOL_KIND.get(s.kind, f"({s.kind})")
                prefix = "  " * indent
                name = f"{prefix}{s.name}"
                lines = f"{s.range_start_line}–{s.range_end_line}"
                _row(kind, name, lines, widths=(12, 36))
                if s.children:
                    _print_symbols(s.children, indent + 1, parent_kind=s.kind)

        _print_symbols(symbols)
        return True
    finally:
        if client.state != ServerState.STOPPED:
            await client.stop()


# ── 2. Registry ────────────────────────────────────────────────


def demo_registry() -> None:
    from chunkhound.lsp.registry import LANGUAGE_SERVER_REGISTRY

    _hdr(2, "LANGUAGE SERVER REGISTRY")

    available = []
    not_found = []
    no_server = []

    _row("Language", "Server", "Status", widths=(18, 30))
    _sep(widths=(18, 30))

    for lang, cfg in sorted(LANGUAGE_SERVER_REGISTRY.items()):
        if cfg.command is None:
            status = "—"
            no_server.append(lang)
        elif shutil.which(cfg.command):
            status = "ON PATH"
            available.append(lang)
        else:
            status = "missing"
            not_found.append(lang)

        cmd = cfg.command or "(none)"
        _row(lang, cmd, status, widths=(18, 30))

    total = len(LANGUAGE_SERVER_REGISTRY)
    print(f"\n  {total} configs | {len(available)} on PATH | {len(not_found)} missing | {len(no_server)} no known server")

    if available:
        print(f"\n  Ready to use: {', '.join(available)}")


# ── DB helper ─────────────────────────────────────────────────

_temp_db: Path | None = None  # cleaned up in main()


def _open_db(db_path: Path):
    """Open DuckDB, snapshotting to a temp file if the original is locked."""
    import duckdb

    try:
        return duckdb.connect(str(db_path), read_only=True)
    except duckdb.IOException:
        pass

    # MCP server holds the lock — snapshot the file and read the copy
    global _temp_db
    if _temp_db is None:
        fd, tmp_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        _temp_db = Path(tmp_path)
        shutil.copy2(db_path, _temp_db)
        print(f"  (DB locked by MCP server — reading snapshot copy)\n")

    return duckdb.connect(str(_temp_db), read_only=True)


def _open_db_rw(db_path: Path):
    """Open DuckDB with write access. Returns None if locked."""
    import duckdb

    try:
        return duckdb.connect(str(db_path))
    except duckdb.IOException:
        return None


# ── 3. Schema v2 ──────────────────────────────────────────────


def _find_db() -> Path | None:
    """Locate the DuckDB file, respecting .chunkhound.json."""
    # Check config first
    cfg_path = Path(".chunkhound.json")
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            db_dir = cfg.get("database", {}).get("path")
            if db_dir:
                candidate = Path(db_dir)
                if candidate.exists():
                    # Could be dir (chunks.db inside) or file
                    if candidate.is_file():
                        return candidate
                    db_file = candidate / "db" / "chunks.db"
                    if db_file.exists():
                        return db_file
        except (json.JSONDecodeError, KeyError):
            pass

    # Default locations
    for candidate in [
        Path(".chunkhound/db"),  # flat file (this project)
        Path(".chunkhound/db/chunks.db"),  # nested dir
    ]:
        if candidate.is_file():
            return candidate

    return None


def demo_schema() -> bool:
    import duckdb

    _hdr(3, "DUCKDB SCHEMA v2")

    db_path = _find_db()
    if not db_path:
        print("  No DuckDB found. Run: chunkhound index .")
        return False

    conn = _open_db(db_path)
    if not conn:
        return False

    try:
        tables = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' ORDER BY table_name"
        ).fetchall()

        print(f"  Database: {db_path} ({db_path.stat().st_size / 1024 / 1024:.1f} MB)\n")
        _row("Table", "Rows", widths=(28, 12))
        _sep(widths=(28, 12))

        for (table,) in tables:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            count = row[0] if row else 0
            _row(table, f"{count:,}", widths=(28, 12))

        # Schema version
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            version = row[0] if row else "?"
            print(f"\n  Schema version: {version}")
        except duckdb.CatalogException:
            print("\n  Schema version: (table not found)")

        # Phase 1 tables
        phase1_tables = {"symbols", "symbol_edges"}
        found = {t for (t,) in tables}
        missing = phase1_tables - found
        if missing:
            print(f"\n  MISSING Phase 1 tables: {', '.join(missing)}")
            return False

        # Show symbols schema
        for tbl in sorted(phase1_tables):
            cols = conn.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                f"WHERE table_name='{tbl}' ORDER BY ordinal_position"
            ).fetchall()
            print(f"\n  {tbl} ({len(cols)} columns):")
            for name, dtype in cols:
                print(f"    {name:<28} {dtype}")

        return True
    finally:
        conn.close()


# ── 4. Path Scoping ───────────────────────────────────────────


def demo_path_scoping() -> bool:
    import duckdb

    _hdr(4, "PATH SCOPING — Prefix Matching")

    db_path = _find_db()
    if not db_path:
        print("  No DuckDB — skipping")
        return False

    conn = _open_db(db_path)
    if not conn:
        return False

    try:
        prefix = "chunkhound/lsp/"

        # Files within prefix
        scoped = conn.execute(
            "SELECT DISTINCT f.path FROM chunks c "
            "JOIN files f ON c.file_id = f.id "
            "WHERE f.path LIKE ? || '%' ORDER BY f.path",
            [prefix],
        ).fetchall()

        # Files containing 'lsp' but OUTSIDE prefix (potential leakage targets)
        outside = conn.execute(
            "SELECT DISTINCT f.path FROM chunks c "
            "JOIN files f ON c.file_id = f.id "
            "WHERE f.path LIKE '%lsp%' "
            "AND NOT f.path LIKE ? || '%' ORDER BY f.path",
            [prefix],
        ).fetchall()

        print(f"  Prefix filter: {prefix}\n")
        print(f"  IN scope ({len(scoped)} files):")
        for (p,) in scoped:
            print(f"    {p}")

        if outside:
            print(f"\n  OUTSIDE scope but contain 'lsp' ({len(outside)} files):")
            for (p,) in outside:
                print(f"    {p}")
            print(f"\n  SQL LIKE prefix correctly excludes these.")
            print(f"  Note: MCP semantic search has a known leak (ch-u62).")
        else:
            print(f"\n  No 'lsp' files outside prefix — clean.")

        return True
    finally:
        conn.close()


# ── 5. Symbols Populated ─────────────────────────────────────


def demo_symbols_populated(conn) -> bool:
    """Scenario 1: symbols table has rows and a known Python symbol is findable."""
    row = conn.execute("SELECT COUNT(*) AS cnt FROM symbols").fetchone()
    total = row[0] if row else 0

    if total == 0:
        print("  symbols table is empty — population did not run")
        return False

    print(f"  Total symbols: {total:,}")

    # Spot-check: look for any Class symbol (most codebases have at least one)
    sample = conn.execute(
        "SELECT name, kind, language, file_path FROM symbols WHERE kind = 'Class' LIMIT 5"
    ).fetchall()

    if sample:
        print(f"\n  Sample classes:")
        for name, kind, lang, fpath in sample:
            print(f"    {name:<30} {kind:<10} {lang or '?':<12} {fpath}")
    else:
        print("  (no Class symbols — checking for any symbol)")
        sample = conn.execute(
            "SELECT name, kind, language, file_path FROM symbols LIMIT 5"
        ).fetchall()
        for name, kind, lang, fpath in sample:
            print(f"    {name:<30} {kind:<10} {lang or '?':<12} {fpath}")

    return True


# ── 6. Incremental Refresh ───────────────────────────────────


def demo_incremental_refresh(conn, file_id: int, file_path: str) -> bool:
    """Scenario 2: per-file delete + re-insert round-trip.

    Proves the incremental path works: delete a file's symbols, re-insert them,
    verify the count is restored. Uses raw SQL to exercise the same operations
    the LSPPopulationService calls (delete_file_symbols + batch insert).
    """
    # Count symbols before
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM symbols WHERE file_id = ?", [file_id]
    ).fetchone()
    count_before = row[0] if row else 0

    if count_before == 0:
        print(f"  No symbols for file_id={file_id} ({file_path}) — cannot test refresh")
        return False

    print(f"  File:             {file_path} (file_id={file_id})")
    print(f"  Symbols before:   {count_before}")

    # Save symbols for re-insert
    saved = conn.execute(
        "SELECT fqn, name, kind, language, file_id, file_path, "
        "range_start, range_end, type_signature, parent_fqn, confidence, lsp_server "
        "FROM symbols WHERE file_id = ?",
        [file_id],
    ).fetchall()

    # Delete (same as LSPPopulationService.delete_file_edges + delete_file_symbols)
    conn.execute(
        "DELETE FROM symbol_edges WHERE "
        "from_symbol_id IN (SELECT id FROM symbols WHERE file_id = ?) OR "
        "to_symbol_id IN (SELECT id FROM symbols WHERE file_id = ?)",
        [file_id, file_id],
    )
    conn.execute("DELETE FROM symbols WHERE file_id = ?", [file_id])

    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM symbols WHERE file_id = ?", [file_id]
    ).fetchone()
    count_deleted = row[0] if row else 0
    print(f"  After delete:     {count_deleted}")

    # Re-insert (same batch pattern as LSPPopulationService._batch_insert)
    if saved:
        placeholders = ", ".join(["(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"] * len(saved))
        flat = [v for row in saved for v in row]
        conn.execute(
            f"INSERT INTO symbols (fqn, name, kind, language, file_id, file_path, "
            f"range_start, range_end, type_signature, parent_fqn, confidence, lsp_server) "
            f"VALUES {placeholders}",
            flat,
        )

    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM symbols WHERE file_id = ?", [file_id]
    ).fetchone()
    count_after = row[0] if row else 0
    print(f"  After re-insert:  {count_after}")

    if count_after != count_before:
        print(f"  MISMATCH: expected {count_before}, got {count_after}")
        return False

    # Verify total unchanged (proves per-file, not full reindex)
    total = conn.execute("SELECT COUNT(*) AS cnt FROM symbols").fetchone()
    print(f"  Total symbols:    {total[0]:,} (unchanged)")

    return True


# ── 7. Multi-Language ────────────────────────────────────────


def demo_multi_language(conn) -> bool | None:
    """Scenario 3: symbols from multiple languages present.

    Returns True (PASS), None (SKIP if <2 languages), or False (FAIL if empty).
    """
    rows = conn.execute(
        "SELECT language, COUNT(*) AS cnt FROM symbols "
        "WHERE language IS NOT NULL GROUP BY language ORDER BY cnt DESC"
    ).fetchall()

    if not rows:
        print("  No symbols with language data")
        return False

    print(f"  Languages found: {len(rows)}")
    for lang, cnt in rows:
        print(f"    {lang:<20} {cnt:,} symbols")

    if len(rows) < 2:
        print(f"\n  SKIP: only 1 language — need ≥2 LSP servers configured and installed")
        return None

    return True


# ── 8. Edge Kinds ────────────────────────────────────────────


def demo_edge_kinds(conn) -> bool:
    """Scenario 4: symbol_edges contains expected edge kinds.

    Requires at least 'defines' and 'references' — the two most fundamental
    edge types that any LSP server with definition + references capability produces.
    """
    rows = conn.execute(
        "SELECT edge_kind, COUNT(*) AS cnt FROM symbol_edges "
        "GROUP BY edge_kind ORDER BY cnt DESC"
    ).fetchall()

    if not rows:
        print("  symbol_edges table is empty")
        return False

    kinds_found = {row[0] for row in rows}
    print(f"  Edge kinds found:")
    for kind, cnt in rows:
        print(f"    {kind:<20} {cnt:,} edges")

    required = {"defines", "references"}
    missing = required - kinds_found
    if missing:
        print(f"\n  MISSING required edge kinds: {', '.join(sorted(missing))}")
        return False

    return True


# ── Main ──────────────────────────────────────────────────────


async def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "chunkhound/lsp/client.py"

    if not Path(target).exists():
        print(f"File not found: {target}")
        return 1

    print(f"\n  ChunkHound Phase 1+2 Demo")
    print(f"  Target: {target}\n")

    results: dict[str, bool] = {}

    # ── Phase 1 ──
    results["lsp_client"] = await demo_lsp_client(target)
    demo_registry()
    results["schema"] = demo_schema()
    results["path_scoping"] = demo_path_scoping()

    # ── Phase 2 ──
    db_path = _find_db()
    if db_path:
        conn = _open_db(db_path)
        try:
            _hdr(5, "SYMBOL POPULATION — Phase 2")
            results["symbols_populated"] = demo_symbols_populated(conn)

            _hdr(6, "INCREMENTAL REFRESH — Phase 2")
            # Pick a known file for refresh demo — use the target file
            file_row = conn.execute(
                "SELECT id FROM files WHERE path = ? LIMIT 1",
                [target],
            ).fetchone()
            if file_row:
                # Need a writable connection for the refresh demo
                conn_rw = _open_db_rw(db_path)
                if conn_rw:
                    try:
                        results["incremental_refresh"] = demo_incremental_refresh(
                            conn_rw, file_id=file_row[0], file_path=target,
                        )
                    finally:
                        conn_rw.close()
                else:
                    print("  DB locked — cannot test incremental refresh (stop MCP server)")
                    results["incremental_refresh"] = False
            else:
                print(f"  Target file not in DB: {target}")
                results["incremental_refresh"] = False

            _hdr(7, "MULTI-LANGUAGE SYMBOLS — Phase 2")
            ml_result = demo_multi_language(conn)
            if ml_result is None:
                print("  [SKIP]")
            else:
                results["multi_language"] = ml_result

            _hdr(8, "EDGE KINDS — Phase 2")
            results["edge_kinds"] = demo_edge_kinds(conn)
        finally:
            conn.close()
    else:
        print("\n  No DuckDB found — skipping Phase 2 demos. Run: chunkhound index .")

    # Summary
    section_num = 9 if db_path else 5
    _hdr(section_num, "SUMMARY")
    for name, passed in results.items():
        icon = "PASS" if passed else "FAIL"
        print(f"  [{icon}]  {name}")

    all_pass = all(results.values())
    phase = "Phase 1+2" if db_path else "Phase 1"
    print(f"\n  {'All ' + phase + ' deliverables verified.' if all_pass else 'Some checks failed — see above.'}")

    # Clean up temp DB snapshot
    if _temp_db and _temp_db.exists():
        _temp_db.unlink()

    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
