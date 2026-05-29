"""Tests for CIMap scripts and released artifacts."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = PACKAGE_ROOT / "scripts"
DOCS = PACKAGE_ROOT / "docs"
DATA = PACKAGE_ROOT / "data"


def run_script(name: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, str(SCRIPTS / name), *args]
    return subprocess.run(
        cmd,
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.fixture(scope="module")
def cimap_docs() -> dict:
    path = DATA / "cimap_docs.json"
    assert path.exists(), "cimap_docs.json missing; run ./build.sh or scripts/repair_docs.py"
    return json.loads(path.read_text(encoding="utf-8"))


class TestPackageLayout:
    def test_required_files_exist(self) -> None:
        required = [
            PACKAGE_ROOT / "README.md",
            PACKAGE_ROOT / "config.yaml",
            PACKAGE_ROOT / "schema.sql",
            PACKAGE_ROOT / "requirements.txt",
            SCRIPTS / "common.py",
            SCRIPTS / "parse_docs.py",
            SCRIPTS / "validate_docs.py",
            SCRIPTS / "repair_docs.py",
            SCRIPTS / "build_database.py",
            DATA / "cimap_docs.json",
            DATA / "stats.json",
            DATA / "cimap_docs.sqlite",
            DOCS,
        ]
        missing = [str(p.relative_to(PACKAGE_ROOT)) for p in required if not p.exists()]
        assert not missing, f"Missing: {missing}"

    def test_no_layer_terminology_in_readme(self) -> None:
        text = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8").lower()
        assert "layer 1" not in text
        assert "layer1" not in text
        assert "layer 2" not in text
        assert "layer 3" not in text

    def test_no_layer_terminology_in_scripts(self) -> None:
        forbidden = ("layer 1", "layer1", "layer 2", "layer2", "layer 3", "layer3")
        for path in SCRIPTS.glob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            hits = [term for term in forbidden if term in text]
            assert not hits, f"{path.name} mentions {hits}"

    def test_no_layer_terminology_in_config(self) -> None:
        text = (PACKAGE_ROOT / "config.yaml").read_text(encoding="utf-8").lower()
        assert "layer" not in text

    def test_docs_source_included(self) -> None:
        assert DOCS.is_dir()
        md_files = list(DOCS.rglob("*.md"))
        assert len(md_files) >= 400

    def test_config_paths_resolve_inside_package(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        try:
            from common import Config, PACKAGE_ROOT as pkg_root

            cfg = Config.load()
            root = pkg_root.resolve()
            for name, path in (
                ("docs_path", cfg.docs_path),
                ("cimap_docs_json", cfg.cimap_docs_json),
                ("sqlite_path", cfg.sqlite_path),
            ):
                resolved = path.resolve()
                assert resolved.is_relative_to(root), f"{name} outside package: {resolved}"
        finally:
            sys.path.pop(0)

    def test_readme_does_not_reference_parent_workspace(self) -> None:
        text = (PACKAGE_ROOT / "README.md").read_text(encoding="utf-8").lower()
        assert "parent repository" not in text
        assert "dataset/scripts/sync" not in text
        assert "replicationpackage/" not in text.replace(" ", "")


class TestReleasedData:
    def test_cimap_docs_structure(self, cimap_docs: dict) -> None:
        assert cimap_docs["schema_version"] >= 1
        assert cimap_docs["page_count"] == 431
        assert "pages" in cimap_docs
        examples = sum(len(p.get("examples", [])) for p in cimap_docs["pages"])
        assert examples == 543

    def test_stats_json_matches_corpus(self, cimap_docs: dict) -> None:
        sys.path.insert(0, str(SCRIPTS))
        try:
            from common import corpus_pair_metrics

            expected = corpus_pair_metrics(cimap_docs)
        finally:
            sys.path.pop(0)

        stats = json.loads((DATA / "stats.json").read_text(encoding="utf-8"))
        assert stats["page_count"] == cimap_docs["page_count"]
        assert stats["example_count"] == 543
        for key in (
            "complete_pairs",
            "parse_ok_pairs",
            "output_lint_ok_pairs",
            "syntactically_clean_pairs",
        ):
            assert stats[key] == expected[key], f"{key}: stats={stats[key]} json={expected[key]}"

        vr = cimap_docs.get("validation_run") or {}
        assert vr.get("skip_external") is False
        assert vr.get("actionlint_path")
        vstats = vr.get("stats") or {}
        assert vstats.get("both_parse_ok") == expected["parse_ok_pairs"]
        assert vstats.get("syntactically_clean_pairs") == expected["syntactically_clean_pairs"]

    def test_repair_metadata_uses_neutral_source(self, cimap_docs: dict) -> None:
        meta = cimap_docs.get("repair_metadata") or {}
        assert meta.get("source") == "docs_extracted.json"


class TestReleasedSqlite:
    def test_shipped_sqlite_matches_json(self, cimap_docs: dict) -> None:
        db_path = DATA / "cimap_docs.sqlite"
        assert db_path.exists() and db_path.stat().st_size > 0

        conn = sqlite3.connect(db_path)
        pages = conn.execute("SELECT COUNT(*) FROM doc_pages").fetchone()[0]
        examples = conn.execute("SELECT COUNT(*) FROM doc_examples").fetchone()[0]
        conn.close()

        assert pages == cimap_docs["page_count"]
        assert examples == sum(len(p.get("examples", [])) for p in cimap_docs["pages"])


class TestBuildDatabase:
    def test_build_sqlite_from_canonical_json(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cimap_docs.sqlite"
        result = run_script("build_database.py", "--fresh", "--output", str(db_path))
        assert result.returncode == 0, result.stderr or result.stdout

        conn = sqlite3.connect(db_path)
        pages = conn.execute("SELECT COUNT(*) FROM doc_pages").fetchone()[0]
        examples = conn.execute("SELECT COUNT(*) FROM doc_examples").fetchone()[0]
        complete = conn.execute(
            "SELECT COUNT(*) FROM v_doc_examples_full WHERE has_pair = 1"
        ).fetchone()[0]
        metrics = {
            "complete_pairs": complete,
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
        conn.close()

        assert pages == 431
        assert examples == 543
        assert metrics == {
            "complete_pairs": 520,
            "parse_ok_pairs": 497,
            "output_lint_ok_pairs": 236,
            "syntactically_clean_pairs": 230,
        }


class TestValidationPipeline:
    def test_validate_docs_parse_only(self, tmp_path: Path) -> None:
        out = tmp_path / "validated.json"
        result = run_script(
            "validate_docs.py",
            "--input",
            str(DATA / "docs_extracted.json"),
            "--output",
            str(out),
            "--skip-external",
        )
        assert result.returncode == 0, result.stderr or result.stdout
        data = json.loads(out.read_text(encoding="utf-8"))
        assert "validation_run" in data
        assert data["validation_run"]["stats"]["examples"] == 543

    def test_repair_docs_parse_only(self, tmp_path: Path) -> None:
        validated = tmp_path / "validated.json"
        run_script(
            "validate_docs.py",
            "--input",
            str(DATA / "docs_extracted.json"),
            "--output",
            str(validated),
            "--skip-external",
        ).check_returncode()

        repaired = tmp_path / "repaired.json"
        repaired_val = tmp_path / "repaired_validated.json"
        canonical = tmp_path / "cimap_docs.json"
        comparison = tmp_path / "comparison.json"

        result = run_script(
            "repair_docs.py",
            "--input",
            str(DATA / "docs_extracted.json"),
            "--repaired-out",
            str(repaired),
            "--validated-out",
            str(repaired_val),
            "--comparison-out",
            str(comparison),
            "--skip-external",
        )
        assert result.returncode == 0, result.stderr or result.stdout
        repaired_data = json.loads(repaired.read_text(encoding="utf-8"))
        assert "repair_metadata" in repaired_data

        comparison_data = json.loads(comparison.read_text(encoding="utf-8"))
        assert comparison_data["after"]["both_parse_ok"] >= comparison_data["before"]["both_parse_ok"]


class TestParseDocs:
    def test_docs_path_points_at_bundled_docs(self) -> None:
        sys.path.insert(0, str(SCRIPTS))
        try:
            from common import Config, PACKAGE_ROOT

            cfg = Config.load()
            assert cfg.docs_path.resolve() == (PACKAGE_ROOT / "docs").resolve()
            assert (cfg.docs_path / "jenkins").is_dir()
        finally:
            sys.path.pop(0)
