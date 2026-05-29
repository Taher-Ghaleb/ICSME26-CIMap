#!/usr/bin/env python3
"""
Enrich docs_extracted.json with syntactic validation metadata.

Reads:  data/docs_extracted.json (or --input)
Writes: data/docs_validated.json (or --output)

For each example:
  - input:  format-aware parse + platform-appropriate linter when available
  - output: YAML parse + actionlint (auto-downloaded to tools/ if missing)

Pure-Python parse checks always run. External linters are best-effort; skipped tools
are recorded in validation metadata (not treated as failure).
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.request import urlopen

import yaml
from tqdm import tqdm

from common import PACKAGE_ROOT, Config, corpus_pair_metrics, load_json, write_json

# actionlint release: https://github.com/rhysd/actionlint/releases
ACTIONLINT_VERSION = "1.7.7"
ACTIONLINT_CACHE = PACKAGE_ROOT / "tools" / "actionlint"
YAMLLINT_DOC_CONFIG = PACKAGE_ROOT / "lint" / "yamllint-doc-fragment.yml"

WORKFLOW_HEADER = """\
name: cimap-lint-fixture
on:
  push:
    branches: [main]
jobs:
  cimap_job:
    runs-on: ubuntu-latest
"""

MAX_LINT_MESSAGES = 12
MAX_MESSAGE_LEN = 400


@dataclass
class CheckResult:
    parse_ok: bool | None = None
    parse_error: str | None = None
    lint_tool: str | None = None
    lint_ok: bool | None = None
    lint_skipped: bool = False
    lint_skip_reason: str | None = None
    lint_messages: list[str] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "parse_ok": self.parse_ok,
            "parse_error": self.parse_error,
            "lint_tool": self.lint_tool,
            "lint_ok": self.lint_ok,
            "lint_skipped": self.lint_skipped,
            "lint_skip_reason": self.lint_skip_reason,
            "lint_messages": self.lint_messages,
            "checks_run": self.checks_run,
        }


def truncate(msg: str) -> str:
    msg = msg.strip().replace("\r\n", "\n")
    if len(msg) > MAX_MESSAGE_LEN:
        return msg[: MAX_MESSAGE_LEN - 3] + "..."
    return msg


def run_subprocess(cmd: list[str], stdin_text: str | None = None, timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def parse_yaml(content: str) -> tuple[bool, str | None]:
    if not content.strip():
        return False, "empty content"
    try:
        list(yaml.safe_load_all(content))
        return True, None
    except yaml.YAMLError as e:
        return False, truncate(str(e))


def parse_json(content: str) -> tuple[bool, str | None]:
    if not content.strip():
        return False, "empty content"
    try:
        json.loads(content)
        return True, None
    except json.JSONDecodeError as e:
        return False, truncate(str(e))


def parse_xml(content: str) -> tuple[bool, str | None]:
    if not content.strip():
        return False, "empty content"
    import xml.etree.ElementTree as ET

    try:
        ET.fromstring(content)
        return True, None
    except ET.ParseError as e:
        return False, truncate(str(e))


def groovy_heuristic(content: str) -> tuple[bool, str | None]:
    """Lightweight sanity check when no Groovy linter is available."""
    if not content.strip():
        return False, "empty content"
    opens = content.count("{") + content.count("(") + content.count("[")
    closes = content.count("}") + content.count(")") + content.count("]")
    if abs(opens - closes) > 3:
        return False, f"unbalanced delimiters (opens≈{opens}, closes≈{closes})"
    return True, None


def input_lint_tool(platform: str, fmt: str) -> str:
    if fmt == "xml":
        return "xmllint"
    if fmt in ("yaml", "json"):
        return "yamllint" if fmt == "yaml" else "json_parse"
    if fmt == "groovy":
        return "groovy_heuristic"
    return "generic_parse"


def find_on_path(name: str) -> str | None:
    path = shutil.which(name)
    if path:
        return path
    if platform.system() == "Windows":
        path = shutil.which(f"{name}.exe")
        if path:
            return path
    return None


def run_yamllint(path: Path) -> tuple[bool | None, list[str], bool, str | None]:
    """Return (lint_ok, messages, skipped, skip_reason)."""
    config_args: list[str] = []
    if YAMLLINT_DOC_CONFIG.is_file():
        config_args = ["-c", str(YAMLLINT_DOC_CONFIG)]

    # Prefer module invocation (pip install yamllint)
    for cmd in (
        [sys.executable, "-m", "yamllint", *config_args, "-f", "parsable", str(path)],
        ["yamllint", *config_args, "-f", "parsable", str(path)],
    ):
        code, out, err = run_subprocess(cmd)
        if code == 127:
            continue
        combined = (out + "\n" + err).strip()
        lines = [ln for ln in combined.splitlines() if ln.strip()]
        if code == 0:
            return True, lines[:MAX_LINT_MESSAGES], False, None
        return False, lines[:MAX_LINT_MESSAGES] or [truncate(combined)], False, None
    return None, [], True, "yamllint not installed (pip install yamllint)"


def run_xmllint(path: Path) -> tuple[bool | None, list[str], bool, str | None]:
    exe = find_on_path("xmllint")
    if not exe:
        return None, [], True, "xmllint not on PATH"
    code, out, err = run_subprocess([exe, "--noout", str(path)])
    combined = (out + "\n" + err).strip()
    lines = [ln for ln in combined.splitlines() if ln.strip()]
    if code == 0:
        return True, lines[:MAX_LINT_MESSAGES], False, None
    return False, lines[:MAX_LINT_MESSAGES] or [truncate(combined)], False, None


def actionlint_reported_version(exe: Path | None) -> str | None:
    if exe is None:
        return None
    code, out, _ = run_subprocess([str(exe), "-version"])
    if code == 0 and out.strip():
        return out.strip().splitlines()[0].strip()
    return ACTIONLINT_VERSION


def ensure_actionlint() -> Path | None:
    """Download actionlint into tools/ if missing."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        asset = f"actionlint_{ACTIONLINT_VERSION}_windows_amd64.zip"
        binary_name = "actionlint.exe"
    elif system == "darwin":
        arch = "arm64" if "arm" in machine else "amd64"
        asset = f"actionlint_{ACTIONLINT_VERSION}_darwin_{arch}.tar.gz"
        binary_name = "actionlint"
    else:
        arch = "arm64" if "arm" in machine else "amd64"
        asset = f"actionlint_{ACTIONLINT_VERSION}_linux_{arch}.tar.gz"
        binary_name = "actionlint"

    dest = ACTIONLINT_CACHE / binary_name
    if dest.is_file():
        return dest

    ACTIONLINT_CACHE.mkdir(parents=True, exist_ok=True)
    url = f"https://github.com/rhysd/actionlint/releases/download/v{ACTIONLINT_VERSION}/{asset}"

    try:
        print(f"Downloading actionlint v{ACTIONLINT_VERSION} ...")
        with urlopen(url, timeout=120) as resp:
            data = resp.read()
    except Exception as e:
        print(f"Warning: could not download actionlint: {e}")
        return find_on_path("actionlint") and Path(find_on_path("actionlint"))  # type: ignore

    tmp = Path(tempfile.mkdtemp(prefix="actionlint-dl-"))
    try:
        archive_path = tmp / asset
        archive_path.write_bytes(data)
        if asset.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(ACTIONLINT_CACHE)
        else:
            import tarfile

            with tarfile.open(archive_path, "r:gz") as tf:
                tf.extractall(ACTIONLINT_CACHE)
        if dest.is_file():
            if system != "windows":
                dest.chmod(0o755)
            return dest
        # tar may extract to subfolder
        for candidate in ACTIONLINT_CACHE.rglob(binary_name):
            if candidate.is_file():
                shutil.copy2(candidate, dest)
                if system != "windows":
                    dest.chmod(0o755)
                return dest
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return find_on_path("actionlint") and Path(find_on_path("actionlint"))  # type: ignore


def wrap_actions_output(content: str) -> str:
    """Wrap doc output fragment in a minimal workflow for actionlint."""
    text = content.strip("\n")
    if not text:
        return WORKFLOW_HEADER + "    steps:\n      - run: echo empty\n"

    # Try structured wrap via YAML
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError:
        doc = None

    if isinstance(doc, list):
        steps_yaml = yaml.dump(doc, default_flow_style=False, sort_keys=False).rstrip()
        indented = "\n".join("      " + ln for ln in steps_yaml.splitlines())
        return WORKFLOW_HEADER + "    steps:\n" + indented + "\n"

    if isinstance(doc, dict):
        if any(k in doc for k in ("uses", "run", "name")):
            return wrap_actions_output(
                yaml.dump([doc], default_flow_style=False, sort_keys=False)
            )
        if "env" in doc and "steps" not in doc:
            job_yaml = yaml.dump(doc, default_flow_style=False, sort_keys=False).rstrip()
            indented = "\n".join("    " + ln for ln in job_yaml.splitlines())
            return WORKFLOW_HEADER + indented + "\n"

    # Raw fragment: indent under steps (preserves comments)
    lines = text.splitlines()
    indented = "\n".join("      " + ln if ln.strip() else "" for ln in lines)
    return WORKFLOW_HEADER + "    steps:\n" + indented + "\n"


def run_actionlint(workflow_path: Path, actionlint_exe: Path | None) -> tuple[bool | None, list[str], bool, str | None]:
    if actionlint_exe is None:
        return None, [], True, "actionlint unavailable"

    code, out, err = run_subprocess([str(actionlint_exe), str(workflow_path)])
    combined = (out + "\n" + err).strip()
    lines = [ln for ln in combined.splitlines() if ln.strip()]
    if code == 0:
        return True, lines[:MAX_LINT_MESSAGES], False, None
    if not lines and combined:
        lines = [truncate(combined)]
    return False, lines[:MAX_LINT_MESSAGES], False, None


def validate_input_block(
    content: str,
    fmt: str,
    platform_slug: str,
    tmp_dir: Path,
    skip_external: bool,
) -> CheckResult:
    res = CheckResult()
    fmt = (fmt or "other").lower()
    res.lint_tool = input_lint_tool(platform_slug, fmt)

    if fmt == "yaml":
        res.checks_run.append("yaml_parse")
        res.parse_ok, res.parse_error = parse_yaml(content)
        if skip_external or not res.parse_ok:
            if skip_external:
                res.lint_skipped = True
                res.lint_skip_reason = "--skip-external"
            return res
        path = tmp_dir / f"in_{platform_slug}.yaml"
        path.write_text(content.replace("\r\n", "\n").rstrip() + "\n", encoding="utf-8")
        res.checks_run.append("yamllint")
        ok, msgs, skipped, reason = run_yamllint(path)
        res.lint_ok = ok
        res.lint_messages = msgs
        res.lint_skipped = skipped
        res.lint_skip_reason = reason
        return res

    if fmt == "json":
        res.checks_run.append("json_parse")
        res.parse_ok, res.parse_error = parse_json(content)
        res.lint_ok = res.parse_ok
        res.lint_skipped = False
        return res

    if fmt == "xml":
        res.checks_run.append("xml_parse")
        res.parse_ok, res.parse_error = parse_xml(content)
        if skip_external or not res.parse_ok:
            if skip_external:
                res.lint_skipped = True
                res.lint_skip_reason = "--skip-external"
            return res
        path = tmp_dir / f"in_{platform_slug}.xml"
        path.write_text(content.replace("\r\n", "\n"), encoding="utf-8")
        res.checks_run.append("xmllint")
        ok, msgs, skipped, reason = run_xmllint(path)
        res.lint_ok = ok
        res.lint_messages = msgs
        res.lint_skipped = skipped
        res.lint_skip_reason = reason
        return res

    if fmt == "groovy":
        res.checks_run.append("groovy_heuristic")
        res.parse_ok, res.parse_error = groovy_heuristic(content)
        res.lint_ok = None
        res.lint_skipped = True
        res.lint_skip_reason = "no Groovy linter bundled; delimiter heuristic only"
        return res

    # shell / other
    res.checks_run.append("non_empty")
    res.parse_ok = bool(content.strip())
    res.parse_error = None if res.parse_ok else "empty content"
    res.lint_ok = None
    res.lint_skipped = True
    res.lint_skip_reason = f"no linter configured for format={fmt}"
    return res


def validate_output_block(
    content: str,
    tmp_dir: Path,
    actionlint_exe: Path | None,
    skip_external: bool,
) -> CheckResult:
    res = CheckResult(lint_tool="actionlint")
    res.checks_run.append("yaml_parse")
    res.parse_ok, res.parse_error = parse_yaml(content)

    if skip_external:
        res.lint_skipped = True
        res.lint_skip_reason = "--skip-external"
        return res

    if not content.strip():
        res.lint_ok = False
        res.lint_messages = ["empty output"]
        return res

    wrapped = wrap_actions_output(content)
    wf_path = tmp_dir / "out_workflow.yml"
    wf_path.write_text(wrapped.replace("\r\n", "\n"), encoding="utf-8")
    res.checks_run.append("actionlint")
    ok, msgs, skipped, reason = run_actionlint(wf_path, actionlint_exe)
    res.lint_ok = ok
    res.lint_messages = msgs
    res.lint_skipped = skipped
    res.lint_skip_reason = reason
    return res


def enrich_corpus(
    data: dict[str, Any],
    skip_external: bool = False,
    download_actionlint: bool = True,
) -> dict[str, Any]:
    actionlint_exe: Path | None = None
    if not skip_external:
        if download_actionlint:
            actionlint_exe = ensure_actionlint()
        else:
            found = find_on_path("actionlint")
            actionlint_exe = Path(found) if found else None
        if actionlint_exe is None:
            print(
                "Warning: actionlint is not available. Output lint will be skipped and "
                "syntactically_clean_pairs will stay 0. Install with: brew install actionlint "
                "(or fix Python SSL certs so auto-download works), then re-run validation."
            )

    validation_run = {
        "tool_versions": {
            "actionlint": actionlint_reported_version(actionlint_exe),
            "yamllint": _yamllint_version(),
        },
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "skip_external": skip_external,
        "actionlint_path": str(actionlint_exe) if actionlint_exe else None,
    }

    stats = {
        "examples": 0,
        "input_parse_ok": 0,
        "input_lint_ok": 0,
        "input_lint_skipped": 0,
        "output_parse_ok": 0,
        "output_lint_ok": 0,
        "output_lint_skipped": 0,
    }

    with tempfile.TemporaryDirectory(prefix="cimap-l1-val-") as tmp:
        tmp_dir = Path(tmp)
        pages = data.get("pages", [])
        for page in tqdm(pages, desc="Validate pages"):
            plat = page.get("platform", "unknown")
            for ex in page.get("examples", []):
                stats["examples"] += 1
                v: dict[str, Any] = {}

                inp = ex.get("input")
                if inp and inp.get("content") is not None:
                    in_res = validate_input_block(
                        inp["content"],
                        inp.get("format", "other"),
                        plat,
                        tmp_dir,
                        skip_external,
                    )
                    v["input"] = in_res.to_dict()
                    if in_res.parse_ok:
                        stats["input_parse_ok"] += 1
                    if in_res.lint_ok is True:
                        stats["input_lint_ok"] += 1
                    if in_res.lint_skipped:
                        stats["input_lint_skipped"] += 1
                else:
                    v["input"] = None

                out = ex.get("output")
                if out and out.get("content") is not None:
                    out_res = validate_output_block(
                        out["content"],
                        tmp_dir,
                        actionlint_exe,
                        skip_external,
                    )
                    v["output"] = out_res.to_dict()
                    if out_res.parse_ok:
                        stats["output_parse_ok"] += 1
                    if out_res.lint_ok is True:
                        stats["output_lint_ok"] += 1
                    if out_res.lint_skipped:
                        stats["output_lint_skipped"] += 1
                else:
                    v["output"] = None

                ex["validation"] = v

    validation_run["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    stats.update(corpus_pair_metrics(data))
    stats["both_parse_ok"] = stats["parse_ok_pairs"]
    validation_run["stats"] = stats

    out_data = dict(data)
    out_data["schema_version"] = max(int(data.get("schema_version", 1)), 2)
    out_data["validation_run"] = validation_run
    return out_data


def _yamllint_version() -> str | None:
    code, out, _ = run_subprocess([sys.executable, "-m", "yamllint", "--version"])
    if code == 0:
        return out.strip().splitlines()[0] if out.strip() else "installed"
    return None


def print_summary(data: dict[str, Any]) -> None:
    vr = data.get("validation_run", {})
    st = vr.get("stats", {})
    n = st.get("examples", 0) or 1
    print("\n=== Validation summary ===")
    print(f"Examples: {n}")
    print(f"  input parse_ok:  {st.get('input_parse_ok', 0)} ({100*st.get('input_parse_ok',0)/n:.1f}%)")
    print(f"  input lint_ok:   {st.get('input_lint_ok', 0)} ({100*st.get('input_lint_ok',0)/n:.1f}%)")
    print(f"  input lint_skip: {st.get('input_lint_skipped', 0)}")
    print(f"  output parse_ok: {st.get('output_parse_ok', 0)} ({100*st.get('output_parse_ok',0)/n:.1f}%)")
    print(f"  output lint_ok:  {st.get('output_lint_ok', 0)} ({100*st.get('output_lint_ok',0)/n:.1f}%)")
    print(f"  output lint_skip:{st.get('output_lint_skipped', 0)}")
    if vr.get("actionlint_path"):
        print(f"  actionlint: {vr['actionlint_path']}")
    if st.get("syntactically_clean_pairs") is not None:
        print(
            f"  syntactically_clean: {st.get('syntactically_clean_pairs', 0)} "
            f"(parse_ok both sides + output lint_ok)"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate documentation corpus and write enriched JSON")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--input", type=Path, default=None, help="docs_extracted.json path")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Default: data/docs_validated.json",
    )
    parser.add_argument(
        "--skip-external",
        action="store_true",
        help="Parse checks only (no yamllint/xmllint/actionlint)",
    )
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="Do not auto-download actionlint; use PATH only",
    )
    args = parser.parse_args()

    cfg = Config.load(args.config)
    in_path = args.input or cfg.extracted_json
    out_path = args.output or cfg.validated_json

    if not in_path.exists():
        raise SystemExit(f"Input not found: {in_path}")

    data = load_json(in_path)
    enriched = enrich_corpus(
        data,
        skip_external=args.skip_external,
        download_actionlint=not args.no_download,
    )
    write_json(out_path, enriched)
    print_summary(enriched)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
