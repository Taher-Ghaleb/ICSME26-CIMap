-- CIMap SQLite schema (built from cimap_docs.json via scripts/build_database.py)
--
-- JSON vs SQL naming:
--   JSON pages[]     -> doc_pages
--   JSON examples[]  -> doc_examples (per-page nested array)
--   JSON input       -> doc_inputs  (at most one row per example)
--   JSON output      -> doc_outputs (at most one row per example)
--
-- Nested JSON is not fully normalized:
--   input.execution_payloads -> doc_inputs.execution_payloads_json (JSON text)
--   example.validation       -> scalar columns on doc_inputs / doc_outputs
--   validation_run (corpus)  -> dataset_builds.validation_run_json
--
-- dataset_builds records build metadata only; it has no FK to doc_* tables.
--
-- Views:
--   v_doc_examples_full   per-example flags (has_pair, parse_ok_pair, syntactically_clean_pair)
--   v_ml_pairs            complete pairs only (INNER JOIN on input and output)
--   v_quality_by_platform aggregates from v_doc_examples_full
--
-- syntactically_clean_pair = parse_ok on both sides AND output lint_ok = 1.
-- When outputs were validated with --skip-external, lint_ok is NULL and this flag is 0.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Reference
-- ---------------------------------------------------------------------------

CREATE TABLE platforms (
    id              INTEGER PRIMARY KEY,
    slug            TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL
);

CREATE TABLE pipeline_contexts (
    id              INTEGER PRIMARY KEY,
    platform_id     INTEGER NOT NULL REFERENCES platforms(id),
    slug            TEXT NOT NULL,
    display_name    TEXT,
    UNIQUE (platform_id, slug)
);

CREATE TABLE dataset_builds (
    id              INTEGER PRIMARY KEY,
    artifact_slug   TEXT NOT NULL DEFAULT 'cimap',
    schema_version  INTEGER NOT NULL,
    source_label    TEXT,
    source_commit   TEXT,
    json_path       TEXT NOT NULL,
    json_sha256     TEXT,
    built_at        TEXT NOT NULL,
    validation_run_json TEXT,
    page_count      INTEGER,
    example_count   INTEGER
);

-- ---------------------------------------------------------------------------
-- Documentation pages and examples
-- ---------------------------------------------------------------------------

CREATE TABLE doc_pages (
    id              INTEGER PRIMARY KEY,
    platform_id     INTEGER NOT NULL REFERENCES platforms(id),
    relative_path   TEXT NOT NULL UNIQUE,
    title           TEXT,
    page_kind       TEXT,
    structure_flags_json TEXT
);

CREATE TABLE doc_examples (
    id                  INTEGER PRIMARY KEY,
    doc_page_id         INTEGER NOT NULL REFERENCES doc_pages(id) ON DELETE CASCADE,
    pipeline_context_id INTEGER REFERENCES pipeline_contexts(id),
    section_title       TEXT,
    example_index       INTEGER NOT NULL DEFAULT 0,
    variant_index       INTEGER NOT NULL DEFAULT 0,
    source_version      TEXT,
    example_kind        TEXT,
    unsupported_options_json TEXT,
    notes               TEXT,
    UNIQUE (doc_page_id, pipeline_context_id, example_index, variant_index)
);

CREATE TABLE doc_inputs (
    id                  INTEGER PRIMARY KEY,
    doc_example_id      INTEGER NOT NULL UNIQUE REFERENCES doc_examples(id) ON DELETE CASCADE,
    format              TEXT NOT NULL,
    content             TEXT NOT NULL,
    content_original    TEXT,
    repair_kinds_json   TEXT,
    content_hash        TEXT NOT NULL,
    parsed_item_json    TEXT,
    execution_payloads_json TEXT,
    -- validation (from validate_docs.py)
    parse_ok            INTEGER,
    parse_error         TEXT,
    lint_tool           TEXT,
    lint_ok             INTEGER,
    lint_skipped        INTEGER NOT NULL DEFAULT 0,
    lint_skip_reason    TEXT,
    lint_messages_json  TEXT,
    checks_run_json     TEXT
);

CREATE TABLE doc_outputs (
    id                  INTEGER PRIMARY KEY,
    doc_example_id      INTEGER NOT NULL UNIQUE REFERENCES doc_examples(id) ON DELETE CASCADE,
    format              TEXT NOT NULL DEFAULT 'yaml',
    content             TEXT NOT NULL,
    content_original    TEXT,
    repair_kinds_json   TEXT,
    content_hash        TEXT NOT NULL,
    parsed_snippet_json TEXT,
    parse_ok            INTEGER,
    parse_error         TEXT,
    lint_tool           TEXT,
    lint_ok             INTEGER,
    lint_skipped        INTEGER NOT NULL DEFAULT 0,
    lint_skip_reason    TEXT,
    lint_messages_json  TEXT,
    checks_run_json     TEXT
);

-- ---------------------------------------------------------------------------
-- Views
-- ---------------------------------------------------------------------------

CREATE VIEW v_doc_examples_full AS
SELECT
    de.id AS example_id,
    dp.relative_path,
    p.slug AS platform,
    dp.title AS page_title,
    dp.page_kind,
    de.section_title,
    de.example_index,
    de.variant_index,
    de.source_version,
    de.example_kind,
    pc.slug AS pipeline_context,
    di.format AS input_format,
    di.content_hash AS input_hash,
    dout.content_hash AS output_hash,
    di.parse_ok AS input_parse_ok,
    di.lint_ok AS input_lint_ok,
    di.lint_skipped AS input_lint_skipped,
    dout.parse_ok AS output_parse_ok,
    dout.lint_ok AS output_lint_ok,
    CASE
        WHEN di.doc_example_id IS NOT NULL AND dout.doc_example_id IS NOT NULL THEN 1
        ELSE 0
    END AS has_pair,
    CASE
        WHEN di.parse_ok = 1 AND dout.parse_ok = 1 THEN 1 ELSE 0
    END AS parse_ok_pair,
    CASE
        WHEN di.parse_ok = 1 AND dout.parse_ok = 1 AND dout.lint_ok = 1 THEN 1 ELSE 0
    END AS syntactically_clean_pair
FROM doc_examples de
JOIN doc_pages dp ON dp.id = de.doc_page_id
JOIN platforms p ON p.id = dp.platform_id
LEFT JOIN pipeline_contexts pc ON pc.id = de.pipeline_context_id
LEFT JOIN doc_inputs di ON di.doc_example_id = de.id
LEFT JOIN doc_outputs dout ON dout.doc_example_id = de.id;

CREATE VIEW v_ml_pairs AS
SELECT
    p.slug || '/' || dp.relative_path || '#' || de.example_index || ':v' || de.variant_index AS pair_id,
    p.slug AS platform,
    dp.relative_path,
    de.section_title,
    de.example_index,
    de.variant_index,
    di.format AS input_format,
    di.content AS source_content,
    dout.content AS target_content,
    de.unsupported_options_json,
    di.parse_ok AS input_parse_ok,
    dout.parse_ok AS output_parse_ok,
    dout.lint_ok AS output_lint_ok
FROM doc_examples de
JOIN doc_pages dp ON dp.id = de.doc_page_id
JOIN platforms p ON p.id = dp.platform_id
JOIN doc_inputs di ON di.doc_example_id = de.id
JOIN doc_outputs dout ON dout.doc_example_id = de.id;

CREATE VIEW v_quality_by_platform AS
SELECT
    platform,
    COUNT(*) AS examples,
    SUM(has_pair) AS complete_pairs,
    SUM(parse_ok_pair) AS parse_ok_pairs,
    SUM(input_parse_ok) AS input_parse_ok,
    SUM(output_parse_ok) AS output_parse_ok,
    SUM(output_lint_ok) AS output_lint_ok,
    SUM(syntactically_clean_pair) AS syntactically_clean_pairs
FROM v_doc_examples_full
GROUP BY platform;
