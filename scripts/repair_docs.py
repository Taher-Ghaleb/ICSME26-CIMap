#!/usr/bin/bin/env python3
"""
Build parse-repaired docs JSON from docs_extracted.json and re-validate.

Writes:
  data/docs_repaired.json
  data/docs_repaired_validated.json
  data/repair_comparison.json
"""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from common import Config, load_json, write_json
from parse_normalize import repair_for_parse
from validate_docs import enrich_corpus, parse_json, parse_xml, parse_yaml, print_summary


def _parse_ok(content: str, fmt: str) -> bool:
    fmt = (fmt or "other").lower()
    if fmt == "yaml":
        ok, _ = parse_yaml(content)
        return ok
    if fmt == "json":
        ok, _ = parse_json(content)
        return ok
    if fmt == "xml":
        ok, _ = parse_xml(content)
        return ok
    return bool(content.strip())


def _accept_repair(original: str, repaired: str, fmt: str, kinds: list[str]) -> tuple[str, list[str]]:
    if original == repaired or not kinds:
        return original, []
    orig_ok = _parse_ok(original, fmt)
    rep_ok = _parse_ok(repaired, fmt)
    if orig_ok and not rep_ok:
        return original, []
    if orig_ok and rep_ok:
        return original, []
    if rep_ok:
        return repaired, kinds
    if not orig_ok and not rep_ok:
        return repaired, kinds
    return original, []


def repair_docs_data(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    out = copy.deepcopy(data)
    stats = {
        "inputs_repaired": 0,
        "outputs_repaired": 0,
        "examples_with_any_repair": 0,
    }

    for page in out.get("pages", []):
        platform = page.get("platform", "unknown")
        for ex in page.get("examples", []):
            any_repair = False
            for side in ("input", "output"):
                block = ex.get(side)
                if not block or block.get("content") is None:
                    continue
                original = block["content"]
                fmt = block.get("format", "yaml" if side == "output" else "other")
                result = repair_for_parse(original, fmt, side, platform)
                block["content_original"] = original
                accepted, kinds = _accept_repair(original, result.text, fmt, result.kinds)
                if accepted != original:
                    block["content"] = accepted
                    block["repair_kinds"] = kinds
                    if side == "input":
                        stats["inputs_repaired"] += 1
                    else:
                        stats["outputs_repaired"] += 1
                    any_repair = True
                else:
                    block["repair_kinds"] = []
            if any_repair:
                stats["examples_with_any_repair"] += 1

    meta = out.setdefault("repair_metadata", {})
    meta["source"] = "docs_extracted.json"
    meta["description"] = (
        "Parse-oriented syntactic repairs on fenced snippets; content_original preserves verbatim docs."
    )
    return out, stats


def validation_parse_stats(data: dict[str, Any]) -> dict[str, int]:
    stats = {
        "examples": 0,
        "with_input": 0,
        "with_output": 0,
        "complete_pairs": 0,
        "input_parse_ok": 0,
        "output_parse_ok": 0,
        "both_parse_ok": 0,
    }
    for page in data.get("pages", []):
        for ex in page.get("examples", []):
            stats["examples"] += 1
            inp = ex.get("input")
            out = ex.get("output")
            iv = (ex.get("validation") or {}).get("input") or {}
            ov = (ex.get("validation") or {}).get("output") or {}
            has_in = bool(inp and inp.get("content"))
            has_out = bool(out and out.get("content"))
            if has_in:
                stats["with_input"] += 1
            if has_out:
                stats["with_output"] += 1
            if has_in and has_out:
                stats["complete_pairs"] += 1
            in_ok = iv.get("parse_ok") is True
            out_ok = ov.get("parse_ok") is True
            if in_ok:
                stats["input_parse_ok"] += 1
            if out_ok:
                stats["output_parse_ok"] += 1
            if has_in and has_out and in_ok and out_ok:
                stats["both_parse_ok"] += 1
    return stats


def compare_stats(before: dict[str, int], after: dict[str, int]) -> dict[str, Any]:
    def delta(key: str) -> int:
        return after.get(key, 0) - before.get(key, 0)

    return {
        "before": before,
        "after": after,
        "delta": {
            "input_parse_ok": delta("input_parse_ok"),
            "output_parse_ok": delta("output_parse_ok"),
            "both_parse_ok": delta("both_parse_ok"),
        },
        "before_rates": {
            "input_parse_ok_pct": round(
                100 * before["input_parse_ok"] / max(before["with_input"], 1), 2
            ),
            "output_parse_ok_pct": round(
                100 * before["output_parse_ok"] / max(before["with_output"], 1), 2
            ),
            "both_parse_ok_pct": round(
                100 * before["both_parse_ok"] / max(before["complete_pairs"], 1), 2
            ),
        },
        "after_rates": {
            "input_parse_ok_pct": round(
                100 * after["input_parse_ok"] / max(after["with_input"], 1), 2
            ),
            "output_parse_ok_pct": round(
                100 * after["output_parse_ok"] / max(after["with_output"], 1), 2
            ),
            "both_parse_ok_pct": round(
                100 * after["both_parse_ok"] / max(after["complete_pairs"], 1), 2
            ),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair parse issues in documentation corpus and re-validate")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--input", type=Path, default=None)
    parser.add_argument("--repaired-out", type=Path, default=None)
    parser.add_argument("--validated-out", type=Path, default=None)
    parser.add_argument("--comparison-out", type=Path, default=None)
    parser.add_argument("--skip-external", action="store_true", help="Parse only (faster)")
    parser.add_argument("--no-download", action="store_true")
    args = parser.parse_args()

    cfg = Config.load(args.config)
    in_path = args.input or cfg.extracted_json
    repaired_path = args.repaired_out or cfg.repaired_json
    validated_path = args.validated_out or cfg.repaired_validated_json
    comparison_path = args.comparison_out or cfg.repair_comparison_json
    baseline_validated = cfg.validated_json

    if not in_path.exists():
        raise SystemExit(f"Missing {in_path}")

    baseline_stats: dict[str, int] | None = None
    if baseline_validated.exists():
        baseline_stats = validation_parse_stats(load_json(baseline_validated))

    source = load_json(in_path)
    repaired, repair_counts = repair_docs_data(source)
    write_json(repaired_path, repaired)
    print(f"Wrote {repaired_path}")
    print(f"  repair counts: {repair_counts}")

    validated = enrich_corpus(
        repaired,
        skip_external=args.skip_external,
        download_actionlint=not args.no_download,
    )
    validated["validation_run"]["repair_pass"] = True
    validated["validation_run"]["repair_counts"] = repair_counts
    write_json(validated_path, validated)
    canonical_path = cfg.cimap_docs_json
    write_json(canonical_path, validated)
    print_summary(validated)
    print(f"\nWrote {validated_path}")
    print(f"Wrote {canonical_path} (canonical release)")

    after_stats = validation_parse_stats(validated)
    if baseline_stats is None:
        baseline_stats = validation_parse_stats(
            enrich_corpus(copy.deepcopy(source), skip_external=True, download_actionlint=False)
        )

    comparison = compare_stats(baseline_stats, after_stats)
    comparison["repair_counts"] = repair_counts
    write_json(comparison_path, comparison)

    print("\n=== Parse repair comparison (baseline -> repaired) ===")
    b, a = comparison["before"], comparison["after"]
    print(f"  input parse_ok:  {b['input_parse_ok']}/{b['with_input']} -> {a['input_parse_ok']}/{a['with_input']}  ({comparison['delta']['input_parse_ok']:+d})")
    print(f"  output parse_ok: {b['output_parse_ok']}/{b['with_output']} -> {a['output_parse_ok']}/{a['with_output']}  ({comparison['delta']['output_parse_ok']:+d})")
    print(f"  both parse_ok:   {b['both_parse_ok']}/{b['complete_pairs']} -> {a['both_parse_ok']}/{a['complete_pairs']}  ({comparison['delta']['both_parse_ok']:+d})")
    print(f"  both rate:       {comparison['before_rates']['both_parse_ok_pct']}% -> {comparison['after_rates']['both_parse_ok_pct']}%")
    print(f"\nWrote {comparison_path}")


if __name__ == "__main__":
    main()
