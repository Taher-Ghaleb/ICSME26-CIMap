"""Shared utilities for the CIMap dataset build scripts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class Config:
    docs_path: Path
    extracted_json: Path
    validated_json: Path
    repaired_json: Path
    repaired_validated_json: Path
    cimap_docs_json: Path
    repair_comparison_json: Path
    sqlite_path: Path
    stats_json: Path

    @classmethod
    def load(cls, config_path: Path | None = None) -> Config:
        config_path = config_path or PACKAGE_ROOT / "config.yaml"
        with open(config_path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        def resolve(p: str) -> Path:
            path = Path(p)
            return path if path.is_absolute() else PACKAGE_ROOT / path

        return cls(
            docs_path=resolve(raw["docs_path"]),
            extracted_json=resolve(raw["extracted_json"]),
            validated_json=resolve(raw["validated_json"]),
            repaired_json=resolve(raw["repaired_json"]),
            repaired_validated_json=resolve(raw["repaired_validated_json"]),
            cimap_docs_json=resolve(raw["cimap_docs_json"]),
            repair_comparison_json=resolve(raw["repair_comparison_json"]),
            sqlite_path=resolve(raw["sqlite_path"]),
            stats_json=resolve(raw["stats_json"]),
        )


PLATFORM_SLUGS = {
    "jenkins": "jenkins",
    "azure_devops": "azure_devops",
    "circle_ci": "circle_ci",
    "gitlab": "gitlab",
    "travis_ci": "travis_ci",
    "bamboo": "bamboo",
    "bitbucket": "bitbucket",
}

PLATFORM_DISPLAY = {
    "jenkins": "Jenkins",
    "azure_devops": "Azure DevOps",
    "circle_ci": "CircleCI",
    "gitlab": "GitLab CI",
    "travis_ci": "Travis CI",
    "bamboo": "Bamboo",
    "bitbucket": "Bitbucket Pipelines",
}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def corpus_pair_metrics(data: dict[str, Any]) -> dict[str, int]:
    """Pair-quality counts from per-example validation in canonical docs JSON."""
    complete = parse_ok = output_lint_ok = syntactically_clean = 0
    for page in data.get("pages", []):
        for ex in page.get("examples", []):
            inp, out = ex.get("input"), ex.get("output")
            if not (
                inp
                and out
                and inp.get("content") is not None
                and out.get("content") is not None
            ):
                continue
            complete += 1
            v = ex.get("validation") or {}
            iv, ov = v.get("input") or {}, v.get("output") or {}
            i_ok = iv.get("parse_ok") is True
            o_ok = ov.get("parse_ok") is True
            o_lint = ov.get("lint_ok") is True
            if i_ok and o_ok:
                parse_ok += 1
            if o_lint:
                output_lint_ok += 1
            if i_ok and o_ok and o_lint:
                syntactically_clean += 1
    return {
        "complete_pairs": complete,
        "parse_ok_pairs": parse_ok,
        "output_lint_ok_pairs": output_lint_ok,
        "syntactically_clean_pairs": syntactically_clean,
    }


def input_execution_payloads(inp: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Doc input block: list of payloads to run (0, 1, or many for CircleCI)."""
    if not inp:
        return []
    payloads = inp.get("execution_payloads")
    if isinstance(payloads, list):
        return payloads
    legacy = inp.get("execution_payload")
    return [legacy] if legacy else []


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def connect_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def apply_schema(conn: sqlite3.Connection, schema_path: Path) -> None:
    conn.executescript(schema_path.read_text(encoding="utf-8"))
