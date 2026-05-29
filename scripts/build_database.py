#!/usr/bin/env python3
"""Build cimap_docs.sqlite from cimap_docs.json and refresh data/stats.json."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from common import (
    PACKAGE_ROOT,
    PLATFORM_DISPLAY,
    Config,
    apply_schema,
    connect_db,
    corpus_pair_metrics,
    input_execution_payloads,
    load_json,
    sha256_text,
    write_json,
)


def seed_platforms(conn: sqlite3.Connection) -> None:
    for slug, display in PLATFORM_DISPLAY.items():
        conn.execute(
            "INSERT OR IGNORE INTO platforms (slug, display_name) VALUES (?, ?)",
            (slug, display),
        )


def platform_id(conn: sqlite3.Connection, slug: str) -> int:
    row = conn.execute("SELECT id FROM platforms WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise KeyError(f"Unknown platform: {slug}")
    return int(row["id"])


def upsert_pipeline_context(
    conn: sqlite3.Connection, platform_slug: str, slug: str
) -> int:
    pid = platform_id(conn, platform_slug)
    conn.execute(
        """
        INSERT OR IGNORE INTO pipeline_contexts (platform_id, slug, display_name)
        VALUES (?, ?, ?)
        """,
        (pid, slug, slug.replace("_", " ").title()),
    )
    row = conn.execute(
        "SELECT id FROM pipeline_contexts WHERE platform_id = ? AND slug = ?",
        (pid, slug),
    ).fetchone()
    return int(row["id"])


def _bool_to_sql(v: bool | None) -> int | None:
    if v is None:
        return None
    return 1 if v else 0


def _validation_cols(v: dict[str, Any] | None) -> dict[str, Any]:
    if not v:
        return {
            "parse_ok": None,
            "parse_error": None,
            "lint_tool": None,
            "lint_ok": None,
            "lint_skipped": 0,
            "lint_skip_reason": None,
            "lint_messages_json": None,
            "checks_run_json": None,
        }
    return {
        "parse_ok": _bool_to_sql(v.get("parse_ok")),
        "parse_error": v.get("parse_error"),
        "lint_tool": v.get("lint_tool"),
        "lint_ok": _bool_to_sql(v.get("lint_ok")),
        "lint_skipped": 1 if v.get("lint_skipped") else 0,
        "lint_skip_reason": v.get("lint_skip_reason"),
        "lint_messages_json": json.dumps(v.get("lint_messages") or []),
        "checks_run_json": json.dumps(v.get("checks_run") or []),
    }


def import_corpus(conn: sqlite3.Connection, data: dict[str, Any]) -> tuple[int, int]:
    pages_n = 0
    examples_n = 0

    for page in data.get("pages", []):
        plat = page["platform"]
        if plat == "shared":
            continue
        pid = platform_id(conn, plat)
        pages_n += 1

        conn.execute(
            """
            INSERT INTO doc_pages (platform_id, relative_path, title, page_kind, structure_flags_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                pid,
                page["relative_path"],
                page.get("title"),
                page.get("page_kind"),
                json.dumps(page.get("structure_flags") or []),
            ),
        )
        doc_page_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        for ex in page.get("examples", []):
            examples_n += 1
            ctx_id = None
            if ex.get("pipeline_context"):
                ctx_id = upsert_pipeline_context(conn, plat, ex["pipeline_context"])

            conn.execute(
                """
                INSERT INTO doc_examples
                (doc_page_id, pipeline_context_id, section_title, example_index, variant_index,
                 source_version, example_kind, unsupported_options_json, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    doc_page_id,
                    ctx_id,
                    ex.get("section_title"),
                    ex.get("example_index", 0),
                    ex.get("variant_index", 0),
                    ex.get("source_version"),
                    ex.get("example_kind"),
                    json.dumps(ex.get("unsupported_options") or []),
                ),
            )
            ex_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            val = ex.get("validation") or {}
            inp = ex.get("input")
            if inp and inp.get("content") is not None:
                payloads = input_execution_payloads(inp)
                parsed_item = None
                if payloads:
                    parsed_item = payloads[0].get("item")
                elif inp.get("parsed_item") is not None:
                    parsed_item = inp.get("parsed_item")
                vc = _validation_cols(val.get("input"))
                conn.execute(
                    """
                    INSERT INTO doc_inputs
                    (doc_example_id, format, content, content_original, repair_kinds_json,
                     content_hash, parsed_item_json, execution_payloads_json, parse_ok, parse_error,
                     lint_tool, lint_ok, lint_skipped, lint_skip_reason, lint_messages_json,
                     checks_run_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ex_id,
                        inp.get("format", "other"),
                        inp["content"],
                        inp.get("content_original"),
                        json.dumps(inp.get("repair_kinds") or [])
                        if inp.get("repair_kinds") is not None
                        else None,
                        inp.get("content_hash") or sha256_text(inp["content"]),
                        json.dumps(parsed_item) if parsed_item is not None else None,
                        json.dumps(payloads) if payloads else None,
                        vc["parse_ok"],
                        vc["parse_error"],
                        vc["lint_tool"],
                        vc["lint_ok"],
                        vc["lint_skipped"],
                        vc["lint_skip_reason"],
                        vc["lint_messages_json"],
                        vc["checks_run_json"],
                    ),
                )

            out = ex.get("output")
            if out and out.get("content") is not None:
                vc = _validation_cols(val.get("output"))
                conn.execute(
                    """
                    INSERT INTO doc_outputs
                    (doc_example_id, format, content, content_original, repair_kinds_json,
                     content_hash, parsed_snippet_json, parse_ok, parse_error, lint_tool, lint_ok,
                     lint_skipped, lint_skip_reason, lint_messages_json, checks_run_json)
                    VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ex_id,
                        out.get("format", "yaml"),
                        out["content"],
                        out.get("content_original"),
                        json.dumps(out.get("repair_kinds") or [])
                        if out.get("repair_kinds") is not None
                        else None,
                        out.get("content_hash") or sha256_text(out["content"]),
                        vc["parse_ok"],
                        vc["parse_error"],
                        vc["lint_tool"],
                        vc["lint_ok"],
                        vc["lint_skipped"],
                        vc["lint_skip_reason"],
                        vc["lint_messages_json"],
                        vc["checks_run_json"],
                    ),
                )

    return pages_n, examples_n


def resolve_input_json(cfg: Config, explicit: Path | None) -> Path:
    if explicit:
        return explicit
    for path in (
        cfg.cimap_docs_json,
        cfg.repaired_validated_json,
        cfg.validated_json,
        cfg.extracted_json,
    ):
        if path.exists():
            return path
    return cfg.cimap_docs_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Build CIMap SQLite")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--input", type=Path, default=None, help="docs JSON (default: canonical if present)")
    parser.add_argument("--output", type=Path, default=None, help="SQLite path")
    parser.add_argument("--fresh", action="store_true", help="Delete existing database first")
    args = parser.parse_args()

    cfg = Config.load(args.config)
    in_path = resolve_input_json(cfg, args.input)
    out_path = args.output or cfg.sqlite_path
    schema_path = PACKAGE_ROOT / "schema.sql"

    if not in_path.exists():
        raise SystemExit(f"Input not found: {in_path}")

    if out_path.exists() and args.fresh:
        out_path.unlink()

    raw_text = in_path.read_text(encoding="utf-8")
    data = json.loads(raw_text)

    conn = connect_db(out_path)
    if args.fresh or conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='platforms'"
    ).fetchone() is None:
        apply_schema(conn, schema_path)
        seed_platforms(conn)

    pages_n, examples_n = import_corpus(conn, data)

    conn.execute(
        """
        INSERT INTO dataset_builds
        (artifact_slug, schema_version, source_label, source_commit, json_path, json_sha256,
         built_at, validation_run_json, page_count, example_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "cimap",
            int(data.get("schema_version", 1)),
            data.get("source"),
            data.get("source_commit"),
            str(in_path),
            sha256_text(raw_text),
            datetime.now(timezone.utc).isoformat(),
            json.dumps(data.get("validation_run")) if data.get("validation_run") else None,
            pages_n,
            examples_n,
        ),
    )

    conn.commit()

    sql_metrics = {
        "complete_pairs": conn.execute(
            "SELECT COUNT(*) FROM v_doc_examples_full WHERE has_pair = 1"
        ).fetchone()[0],
        "parse_ok_pairs": conn.execute(
            "SELECT COUNT(*) FROM v_doc_examples_full WHERE parse_ok_pair = 1"
        ).fetchone()[0],
        "output_lint_ok_pairs": conn.execute(
            """
            SELECT COUNT(*) FROM v_doc_examples_full
            WHERE has_pair = 1 AND output_lint_ok = 1
            """
        ).fetchone()[0],
        "syntactically_clean_pairs": conn.execute(
            "SELECT COUNT(*) FROM v_doc_examples_full WHERE syntactically_clean_pair = 1"
        ).fetchone()[0],
    }
    json_metrics = corpus_pair_metrics(data)
    if json_metrics != sql_metrics:
        print("Warning: SQLite counts differ from JSON validation metadata:")
        for key in json_metrics:
            if json_metrics[key] != sql_metrics[key]:
                print(f"  {key}: json={json_metrics[key]} sqlite={sql_metrics[key]}")

    stats_summary = {
        "artifact": "cimap",
        "page_count": pages_n,
        "example_count": examples_n,
        **sql_metrics,
        "source": data.get("source"),
    }
    write_json(cfg.stats_json, stats_summary)

    conn.close()

    print(f"Input:  {in_path}")
    print(f"Output: {out_path}")
    print(f"Stats:  {cfg.stats_json}")
    print(f"  doc_pages: {pages_n}")
    print(f"  doc_examples: {examples_n}")
    for key in (
        "complete_pairs",
        "parse_ok_pairs",
        "output_lint_ok_pairs",
        "syntactically_clean_pairs",
    ):
        print(f"  {key}: {sql_metrics[key]}")


if __name__ == "__main__":
    main()
