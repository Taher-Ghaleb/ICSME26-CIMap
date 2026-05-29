"""
Platform-specific parsers: doc input text → execution payload for actions_importer transformers.

Each parser returns a dict:
  identifier: str | None     — transformer IDENTIFIER lookup
  item: Any                  — passed as transform(item: ...)
  variables: dict | None     — passed as transform(variables: ...) for Bitbucket pipes
  resources: dict            — optional Azure checkout resources
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml


def xml_to_item_dict(xml_text: str) -> dict[str, Any] | None:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None

    def elem_to_obj(elem: ET.Element) -> Any:
        children = list(elem)
        if not children:
            return (elem.text or "").strip()
        result: dict[str, Any] = {}
        for child in children:
            key = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            val = elem_to_obj(child)
            if key in result:
                if not isinstance(result[key], list):
                    result[key] = [result[key]]
                result[key].append(val)
            else:
                result[key] = val
        return result

    tag = root.tag.split("}")[-1] if "}" in root.tag else root.tag
    body = elem_to_obj(root)
    if isinstance(body, dict):
        body["_root_tag"] = tag
        return body
    return {"_root_tag": tag, "_value": body}

# Doc page → default identifier when YAML does not embed task/pipe name
DOC_PAGE_IDENTIFIERS: dict[tuple[str, str], str] = {
    ("circle_ci", "Steps/Run.md"): "run",
    ("circle_ci", "Steps/Checkout.md"): "checkout",
    ("circle_ci", "Steps/SaveCache.md"): "save_cache",
    ("circle_ci", "Steps/RestoreCache.md"): "restore_cache",
    ("circle_ci", "Steps/StoreArtifacts.md"): "store_artifacts",
    ("circle_ci", "Steps/StoreTestResults.md"): "store_test_results",
    ("circle_ci", "Steps/PersistToWorkspace.md"): "persist_to_workspace",
    ("circle_ci", "Steps/AttachWorkspace.md"): "attach_workspace",
    ("circle_ci", "Steps/AddSshKeys.md"): "add_ssh_keys",
    ("circle_ci", "Steps/When.md"): "when",
    ("circle_ci", "Steps/Unless.md"): "unless",
    ("gitlab", "Script.md"): "script",
    ("gitlab", "BeforeScript.md"): "before_script",
    ("gitlab", "AfterScript.md"): "after_script",
    ("gitlab", "Cache.md"): "cache",
    ("gitlab", "Checkout.md"): "checkout",
    ("gitlab", "Artifacts.md"): "artifacts",
    ("gitlab", "Image.md"): "image",
    ("gitlab", "Services.md"): "services",
    ("gitlab", "Timeout.md"): "timeout",
    ("gitlab", "Environment.md"): "environment",
    ("gitlab", "Trigger.md"): "trigger",
    ("gitlab", "Pages.md"): "pages",
    ("gitlab", "Release.md"): "release",
    ("gitlab", "Dependencies.md"): "dependencies",
    ("gitlab", "Secrets.md"): "secrets",
    ("gitlab", "Tags.md"): "tags",
    ("travis_ci", "Scripts.md"): "script",
    ("travis_ci", "Env.md"): "env",
    ("travis_ci", "Git.md"): "git",
    ("travis_ci", "Language.md"): "language",
    ("travis_ci", "Cache.md"): "cache",
    ("travis_ci", "Services.md"): "services",
    ("jenkins", "Shell.md"): "hudson.tasks.Shell",
    ("jenkins", "Checkout.md"): "hudson.plugins.git.GitSCM",
    ("bitbucket", "pipes/aws_sam_deploy.md"): "atlassian/aws-sam-deploy",
}


def default_identifier(platform: str, relative_path: str) -> str | None:
    return DOC_PAGE_IDENTIFIERS.get((platform, relative_path))


def parse_input_for_platform(
    platform: str,
    fmt: str,
    content: str,
    relative_path: str = "",
) -> dict[str, Any] | None:
    """Return execution payload or None if not parseable."""
    parsers = {
        "jenkins": _parse_jenkins,
        "azure_devops": _parse_azure_devops,
        "circle_ci": _parse_circle_ci,
        "gitlab": _parse_gitlab,
        "travis_ci": _parse_travis_ci,
        "bamboo": _parse_bamboo,
        "bitbucket": _parse_bitbucket,
    }
    fn = parsers.get(platform)
    if not fn:
        return None
    try:
        payload = fn(content, fmt, relative_path)
    except Exception:
        return None
    if not payload:
        return None
    if not payload.get("identifier"):
        hint = default_identifier(platform, relative_path)
        if hint:
            payload["identifier"] = hint
    if payload.get("resources"):
        pass
    else:
        payload.pop("resources", None)
    return payload


def _load_yaml(content: str) -> Any:
    data = yaml.safe_load(content)
    if data is None:
        return None
    return data


_JENKINS_XML_WRAPPERS = frozenset(
    {"builders", "buildWrappers", "publishers", "trigger", "scm", "buildTriggers"}
)


def _unwrap_jenkins_xml_item(item: dict[str, Any], ident: str) -> tuple[str, dict[str, Any]]:
    """Use inner plugin tag when XML is wrapped (e.g. <builders><XCodeBuilder>)."""
    if ident not in _JENKINS_XML_WRAPPERS:
        return ident, item
    keys = [k for k in item if not k.startswith("_")]
    if len(keys) != 1:
        return ident, item
    child = keys[0]
    val = item[child]
    if isinstance(val, dict):
        return child, val
    return ident, item


def _parse_jenkins(content: str, fmt: str, relative_path: str) -> dict[str, Any] | None:
    if fmt == "xml":
        item = xml_to_item_dict(content)
        if not item:
            return None
        ident = item.get("_root_tag")
        if ident:
            del item["_root_tag"]
        if not ident:
            return None
        ident, item = _unwrap_jenkins_xml_item(item, ident)
        return {"identifier": ident, "item": item}
    return None


def _parse_azure_devops(content: str, fmt: str, relative_path: str) -> dict[str, Any] | None:
    if fmt not in ("yaml", "yml", "other"):
        return None
    data = _load_yaml(content)
    if data is None:
        return None

    step = data[0] if isinstance(data, list) else data
    if not isinstance(step, dict):
        return None

    # - task: Ant@1
    if "task" in step:
        task = str(step["task"])
        inputs = step.get("inputs") or {}
        if not isinstance(inputs, dict):
            inputs = {}
        return {"identifier": task, "item": inputs}

    # - checkout: self | none | repo
    if "checkout" in step:
        val = step["checkout"]
        item = {"repository": val if isinstance(val, str) else "self"}
        for k, v in step.items():
            if k != "checkout":
                item[_camel_key(k)] = v
        return {"identifier": "checkout", "item": item}

    # - script: / - bash: / - pwsh: / - powershell:
    for key in ("script", "bash", "powershell", "pwsh"):
        if key in step:
            ident = "script" if key in ("script", "bash") else "PowerShell@2"
            item = {"script": step[key]} if key in ("script", "bash") else {
                "targetType": "inline",
                "script": step[key],
            }
            for k, v in step.items():
                if k != key:
                    item[_camel_key(k)] = v
            return {"identifier": ident, "item": item}

    # - publish: / - download: pipeline artifact shorthand
    if "publish" in step:
        return {"identifier": "publish", "item": step}
    if "download" in step:
        return {"identifier": "download", "item": step}

    return None


def _camel_key(k: str) -> str:
    if k == "working-directory":
        return "workingDirectory"
    parts = k.replace("-", "_").split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _parse_circle_ci(content: str, fmt: str, relative_path: str) -> dict[str, Any] | None:
    from circle_ci_parser import extract_circle_ci_payloads

    payloads = extract_circle_ci_payloads(content, relative_path)
    if payloads:
        return payloads[0]
    return None


def parse_circle_ci_payloads(content: str, relative_path: str) -> list[dict[str, Any]]:
    from circle_ci_parser import extract_circle_ci_payloads

    return extract_circle_ci_payloads(content, relative_path)


def _parse_gitlab(content: str, fmt: str, relative_path: str) -> dict[str, Any] | None:
    data = _load_yaml(content)
    if data is None or not isinstance(data, dict):
        return None

    script_keys = (
        "script",
        "before_script",
        "after_script",
        "cache",
        "artifacts",
        "image",
        "services",
        "variables",
        "timeout",
        "environment",
        "trigger",
        "pages",
        "release",
        "dependencies",
        "secrets",
        "tags",
    )
    for key in script_keys:
        if key in data:
            return {"identifier": key, "item": data[key]}

    if len(data) == 1:
        key, val = next(iter(data.items()))
        return {"identifier": key, "item": val}

    return None


def _parse_travis_ci(content: str, fmt: str, relative_path: str) -> dict[str, Any] | None:
    data = _load_yaml(content)
    if data is None:
        # env: FOO=foo single line
        m = re.match(r"^(\w+):\s*(.+)$", content.strip(), re.DOTALL)
        if m:
            key, val = m.group(1), m.group(2).strip()
            return {"identifier": key, "item": _parse_travis_scalar(key, val)}
        return None

    if isinstance(data, dict):
        if len(data) == 1:
            key, val = next(iter(data.items()))
            return {"identifier": key, "item": _parse_travis_value(key, val)}
        ident = default_identifier("travis_ci", relative_path)
        if ident:
            return {"identifier": ident, "item": data}
    return None


def _parse_travis_scalar(key: str, val: str) -> Any:
    if key == "env" and "=" in val and not val.startswith("-"):
        return {val.split("=", 1)[0]: val.split("=", 1)[1]}
    return val


def _parse_travis_value(key: str, val: Any) -> Any:
    if key == "env":
        if isinstance(val, list):
            out: dict[str, Any] = {}
            for el in val:
                if isinstance(el, dict):
                    out.update(el)
                elif isinstance(el, str) and "=" in el:
                    k, v = el.split("=", 1)
                    out[k.strip()] = v.strip()
            return out
        return val
    return val


def _parse_bamboo(content: str, fmt: str, relative_path: str) -> dict[str, Any] | None:
    data = _load_yaml(content)
    if data is None or not isinstance(data, dict):
        return None

    tasks = data.get("tasks")
    if isinstance(tasks, list):
        for task in tasks:
            if not isinstance(task, dict):
                continue
            if len(task) == 1:
                name, body = next(iter(task.items()))
                ident = name
                if name == "maven":
                    ident = "maven"
                return {
                    "identifier": ident,
                    "item": body if isinstance(body, dict) else {"value": body},
                }

    # Trigger-style docs
    if len(data) == 1:
        key, val = next(iter(data.items()))
        return {"identifier": key, "item": val if isinstance(val, dict) else {"value": val}}

    stem = Path(relative_path).stem.lower()
    if stem == "maven":
        return {"identifier": "maven", "item": data}
    return None


def _parse_bitbucket(content: str, fmt: str, relative_path: str) -> dict[str, Any] | None:
    data = _load_yaml(content)
    if data is None:
        return None

    steps = data if isinstance(data, list) else [data]
    for step in steps:
        if not isinstance(step, dict):
            continue
        if "pipe" in step:
            pipe = str(step["pipe"]).split(":")[0]  # atlassian/aws-sam-deploy
            variables = step.get("variables") or {}
            if not isinstance(variables, dict):
                variables = {}
            return {
                "identifier": pipe,
                "variables": variables,
                "item": variables,
            }
        if "script" in step:
            return {"identifier": "script", "item": step["script"]}

    ident = default_identifier("bitbucket", relative_path)
    if ident:
        return {"identifier": ident, "variables": data if isinstance(data, dict) else {}, "item": data}
    return None
