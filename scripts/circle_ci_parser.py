"""
Extract CircleCI doc YAML into execution payloads (orb commands, steps, jobs, executors, triggers).
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

# Orb command identifiers: circleci_{orb}_{command} and circleci_{orb}_job_{command}
_ORB_COMMAND_IDENTS: set[str] | None = None

# Doc folder name -> gem identifier orb prefix (not always pascal_to_snake(folder))
ORB_DOC_FOLDER_PREFIX: dict[str, str] = {
    "Cypress": "cypress_io_cypress",
}


def pascal_to_snake(name: str) -> str:
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", name)
    return s.lower().replace("-", "_")


def orb_base_from_folder(folder: str) -> str:
    if folder in ORB_DOC_FOLDER_PREFIX:
        return ORB_DOC_FOLDER_PREFIX[folder]
    snake = pascal_to_snake(folder)
    known = _load_orb_idents()
    if known and any(i.startswith(f"circleci_{snake}_") for i in known):
        return f"circleci_{snake}"
    return snake


def identifier_from_doc_path(relative_path: str) -> str | None:
    """Map docs path to transformer IDENTIFIER when YAML is ambiguous."""
    if m := re.match(r"circle_ci/Orbs/([^/]+)/([^/]+)\.md$", relative_path):
        orb, cmd = m.group(1), m.group(2)
        if cmd.lower() == "executors":
            return None
        orb_s = orb_base_from_folder(orb)
        cmd_s = pascal_to_snake(cmd)
        known = _load_orb_idents()
        job_ident = f"{orb_s}_job_{cmd_s}"
        cmd_ident = f"{orb_s}_{cmd_s}"
        if known:
            if job_ident in known:
                return job_ident
            if cmd_ident in known:
                return cmd_ident
            # Install.md -> setup class also registers cypress_io_cypress_install
            for alt in (cmd_ident, f"circleci_{pascal_to_snake(orb)}_{cmd_s}"):
                if alt in known:
                    return alt
        return cmd_ident

    if m := re.match(r"circle_ci/Steps/([^/]+)\.md$", relative_path):
        return pascal_to_snake(m.group(1))

    if m := re.match(r"circle_ci/Triggers/([^/]+)\.md$", relative_path):
        return pascal_to_snake(m.group(1))

    if m := re.match(r"circle_ci/Executors/([^/]+)\.md$", relative_path):
        return pascal_to_snake(m.group(1))

    return None


def _load_orb_idents() -> set[str]:
    global _ORB_COMMAND_IDENTS
    if _ORB_COMMAND_IDENTS is not None:
        return _ORB_COMMAND_IDENTS
    _ORB_COMMAND_IDENTS = set()
    # Orb identifier hints are optional; docs-only package builds without static maps.
    return _ORB_COMMAND_IDENTS


def _alias_prefix(alias: str) -> str:
    return alias.replace("-", "_").lower()


def orb_ident_prefix(orb_alias: str, orb_spec: str | None = None) -> str:
    spec = (orb_spec or "").lower()
    if "cypress-io" in spec or orb_alias == "cypress":
        return "cypress_io_cypress"
    if "circleci/" in spec:
        name = spec.split("circleci/")[1].split("@")[0].replace("-", "_")
        return f"circleci_{name}"
    if orb_alias in ("go", "heroku", "aws", "node", "python", "ruby", "slack", "browser-tools"):
        return f"circleci_{_alias_prefix(orb_alias)}"
    return _alias_prefix(orb_alias)


def identifier_for_orb_step(
    orb_alias: str,
    command: str,
    relative_path: str,
    orb_spec: str | None = None,
) -> str:
    prefix = orb_ident_prefix(orb_alias, orb_spec)
    cmd = command.replace("-", "_").lower()
    job_first = cmd == "run"
    pair = (f"{prefix}_job_{cmd}", f"{prefix}_{cmd}")
    candidates = list(reversed(pair) if job_first else pair) + [
        f"circleci_{_alias_prefix(orb_alias)}_job_{cmd}",
        f"circleci_{_alias_prefix(orb_alias)}_{cmd}",
    ]
    known = _load_orb_idents()
    for c in candidates:
        if known and c in known:
            return c
    doc_ident = identifier_from_doc_path(relative_path)
    if doc_ident and (not known or doc_ident in known):
        return doc_ident
    return candidates[0]


def executor_field_to_identifier(executor: str, orb_aliases: dict[str, str]) -> str | None:
    if not isinstance(executor, str):
        return None
    if "/" in executor:
        orb_part, tag = executor.split("/", 1)
        prefix = orb_ident_prefix(orb_part, orb_aliases.get(orb_part))
        tag_s = tag.replace(".", "_").replace("-", "_")
        ident = f"{prefix}_{tag_s}"
        known = _load_orb_idents()
        if known and ident in known:
            return ident
        # cypress/base-6 -> cypress_io_cypress_base_6
        alt = f"{prefix}_{tag_s.replace('_', '_')}"
        if known and alt in known:
            return alt
        return ident
    return None


def _normalize_steps(steps: Any) -> list[Any]:
    if steps is None:
        return []
    if isinstance(steps, list):
        return steps
    return [steps]


def _step_to_payload(
    step: Any,
    relative_path: str,
    orb_aliases: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    orb_aliases = orb_aliases or {}
    if isinstance(step, str):
        ident = step.strip()
        if "/" in ident:
            orb_alias, command = ident.split("/", 1)
            resolved = identifier_for_orb_step(
                orb_alias, command, relative_path, orb_aliases.get(orb_alias)
            )
            return {"identifier": resolved, "item": {}, "resources": {}}
        return {"identifier": ident, "item": {}, "resources": {}}

    if not isinstance(step, dict):
        return None

    if len(step) == 1:
        key, val = next(iter(step.items()))
        item = val if isinstance(val, dict) else ({} if val is None else {"value": val})

        if "/" in str(key):
            orb_alias, command = str(key).split("/", 1)
            ident = identifier_for_orb_step(
                orb_alias, command, relative_path, orb_aliases.get(orb_alias)
            )
            return {"identifier": ident, "item": item, "resources": {}}

        ident = str(key)
        # Native steps use keys like save_cache, checkout
        if ident == "run" and isinstance(val, str):
            return {"identifier": "run", "item": val, "resources": {}}
        return {"identifier": ident, "item": item, "resources": {}}

    # Multi-key step (unusual)
    if "command" in step or "run" in step:
        return {"identifier": "run", "item": step, "resources": {}}
    return None


def _workflow_jobs_to_payloads(
    jobs: Any,
    relative_path: str,
    orb_aliases: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    orb_aliases = orb_aliases or {}
    payloads: list[dict[str, Any]] = []
    if jobs is None:
        return payloads

    entries: list[Any]
    if isinstance(jobs, list):
        entries = jobs
    elif isinstance(jobs, dict):
        entries = [{k: v} for k, v in jobs.items()]
    else:
        return payloads

    for entry in entries:
        if isinstance(entry, str):
            if "/" in entry:
                orb, cmd = entry.split("/", 1)
                ident = identifier_for_orb_step(
                    orb, cmd, relative_path, orb_aliases.get(orb)
                )
                payloads.append({"identifier": ident, "item": {}, "resources": {}})
            continue
        if not isinstance(entry, dict) or len(entry) != 1:
            continue
        key, val = next(iter(entry.items()))
        item = val if isinstance(val, dict) else {}
        if "/" in str(key):
            orb, cmd = str(key).split("/", 1)
            ident = identifier_for_orb_step(
                orb, cmd, relative_path, orb_aliases.get(orb)
            )
            prefix = orb_ident_prefix(orb, orb_aliases.get(orb))
            job_ident = f"{prefix}_job_{cmd.replace('-', '_').lower()}"
            known = _load_orb_idents()
            if known and job_ident in known:
                ident = job_ident
            payloads.append({"identifier": ident, "item": item, "resources": {}})
    return payloads


def _top_level_payloads(data: dict[str, Any], relative_path: str) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []

    if "docker" in data and "Executors" in relative_path:
        docker = data["docker"]
        item = docker[0] if isinstance(docker, list) and docker else docker
        payloads.append({"identifier": "docker", "item": item, "resources": {}})

    if "machine" in data and "Executors" in relative_path:
        payloads.append({"identifier": "machine", "item": data["machine"], "resources": {}})

    if "macos" in data and "Executors" in relative_path:
        payloads.append({"identifier": "macos", "item": data["macos"], "resources": {}})

    if "triggers" in data and "Triggers" in relative_path:
        triggers = data["triggers"]
        if isinstance(triggers, list):
            for tr in triggers:
                if isinstance(tr, dict):
                    for k, v in tr.items():
                        payloads.append(
                            {
                                "identifier": k,
                                "item": v if isinstance(v, dict) else {},
                                "resources": {},
                            }
                        )

    if "steps" in data and "Steps" in relative_path:
        for step in _normalize_steps(data["steps"]):
            p = _step_to_payload(step, relative_path)
            if p:
                payloads.append(p)

    if "run" in data and "Steps" in relative_path:
        merged = _merge_run_value(data["run"])
        if merged is not None:
            payloads.append(
                {
                    "identifier": "run",
                    "item": merged if isinstance(merged, dict) else merged,
                    "resources": {},
                }
            )

    if "schedule" in data or "parameters" in data:
        for key in ("schedule", "parameters"):
            if key in data:
                payloads.append(
                    {
                        "identifier": key,
                        "item": data[key] if isinstance(data[key], dict) else {},
                        "resources": {},
                    }
                )

    return payloads


def _merge_run_value(run_val: Any) -> Any:
    if isinstance(run_val, str):
        return run_val
    if isinstance(run_val, dict):
        return run_val
    if isinstance(run_val, list):
        merged: dict[str, Any] = {}
        for el in run_val:
            if isinstance(el, dict):
                merged.update(el)
            elif isinstance(el, str):
                return el
        return merged if merged else None
    return run_val


def extract_circle_ci_payloads(
    content: str,
    relative_path: str,
) -> list[dict[str, Any]]:
    """
    Parse a CircleCI doc input YAML block into zero or more execution payloads.
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError:
        return []

    if data is None:
        return []

    payloads: list[dict[str, Any]] = []
    doc_fallback = identifier_from_doc_path(relative_path)

    orb_aliases: dict[str, str] = {}
    if isinstance(data, dict):
        raw_orbs = data.get("orbs") or {}
        if isinstance(raw_orbs, dict):
            for alias, spec in raw_orbs.items():
                if isinstance(spec, str):
                    orb_aliases[str(alias)] = spec

    if isinstance(data, dict):
        # jobs.*.steps and executor fields
        for job_name, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            if "executor" in job and "Executors" in relative_path:
                ident = executor_field_to_identifier(job["executor"], orb_aliases)
                if ident:
                    payloads.append({"identifier": ident, "item": {}, "resources": {}})
            for step in _normalize_steps(job.get("steps")):
                p = _step_to_payload(step, relative_path, orb_aliases)
                if p:
                    payloads.append(p)

        # workflows.*.jobs (orb job invocations)
        for _wf, wf in (data.get("workflows") or {}).items():
            if not isinstance(wf, dict):
                continue
            payloads.extend(
                _workflow_jobs_to_payloads(wf.get("jobs"), relative_path, orb_aliases)
            )

        payloads.extend(_top_level_payloads(data, relative_path))

    elif isinstance(data, list):
        for el in data:
            p = _step_to_payload(el, relative_path, orb_aliases)
            if p:
                payloads.append(p)

    # Prefer doc-path identifier when step YAML uses wrong orb command (e.g. SaveCache doc shows load-cache)
    doc_ident = identifier_from_doc_path(relative_path)
    if doc_ident:
        known = _load_orb_idents()
        if known and doc_ident in known:
            for p in payloads:
                ident = p.get("identifier")
                if ident and (not known or ident not in known):
                    p["identifier"] = doc_ident

    # Deduplicate by identifier + item json
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for p in payloads:
        key = f"{p.get('identifier')}:{json.dumps(p.get('item'), sort_keys=True, default=str)}"
        if key in seen:
            continue
        seen.add(key)
        unique.append(p)

    if not unique and doc_fallback:
        item: Any = data if isinstance(data, dict) else {}
        unique.append(
            {
                "identifier": doc_fallback,
                "item": item,
                "resources": {},
            }
        )

    return unique
