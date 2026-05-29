#!/usr/bin/env python3
"""
Parse docs/**/*.md into structured JSON (or validate existing docs_extracted.json).

Input section headers vary by platform; output sections similarly.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from common import Config, PLATFORM_SLUGS, input_execution_payloads, sha256_text, write_json

sys.path.insert(0, str(Path(__file__).resolve().parent))
from input_parsers import parse_circle_ci_payloads, parse_input_for_platform, xml_to_item_dict  # noqa: E402

INPUT_HEADERS = {
    "jenkins input",
    "azure devops input",
    "travis input",
    "circleci input",
    "gitlab input",
    "bamboo input",
    "bitbucket input",
    "input",
}

OUTPUT_HEADERS = {
    "transformed github action",
    "transformed github actions",
    "transformed github workflow",
    "transformed gitlab action",
}

UNSUPPORTED_HEADERS = {
    "unsupported options",
    "unsupported inputs",
    "unsupported inputs and aliases",
    "unsupported",
}

HEADING_RE = re.compile(r"^(#{2,4})\s+(.+)$")
# Azure catalog prose labels before alternate task major versions (Docker.md: V1, V2, V0).
DOC_VERSION_LABEL_RE = re.compile(r"^V(\d+)$", re.IGNORECASE)


def header_mode(title: str) -> str | None:
    h = title.lower().strip()
    if h in INPUT_HEADERS:
        return "input"
    if any(h == o or h.startswith(o + " ") or h.startswith(o) for o in OUTPUT_HEADERS):
        return "output"
    if any(
        h == u
        or h.startswith(u + " ")
        or h.startswith(u)
        for u in UNSUPPORTED_HEADERS
    ):
        return "unsupported"
    for token in INPUT_HEADERS:
        if token == "input":
            continue
        if token in h:
            return "input"
    return None


def detect_platform(rel_path: Path) -> str:
    top = rel_path.parts[0] if rel_path.parts else ""
    if top == "gitlab":
        return "gitlab"
    return PLATFORM_SLUGS.get(top, top)


def slugify_pipeline_context(title: str) -> str:
    t = title.strip().lower()
    t = re.sub(r"[^a-z0-9]+", "_", t)
    return t.strip("_") or "default"


def page_short_name(title: str, rel: Path) -> str:
    """Short label from H1 or filename (e.g. 'Docker Task' -> 'Docker')."""
    t = re.sub(r"\s+(Task|Plugin|Builder|Wrapper|Pipeline)s?\s*$", "", title, flags=re.I).strip()
    if t:
        return t
    stem = rel.stem.replace("_", " ")
    return stem[:1].upper() + stem[1:] if stem else rel.stem


def is_topic_heading(title: str) -> bool:
    """### / #### titles that name a command or plugin slice (Login, Build, Xcode)."""
    h = title.lower().strip()
    if header_mode(title) is not None:
        return False
    if "transformed" in h and ("action" in h or "workflow" in h):
        return False
    if "unsupported" in h or "manual task" in h:
        return False
    if "partially supported" in h or "supported options" in h:
        return False
    if h in ("conditions", "orb mappings", "trigger mappings", "multiple versions"):
        return False
    if "pipeline" in h and ("designer" in h or "jenkinsfile" in h):
        return False
    if h.endswith(" input") or h.endswith(" inputs"):
        return False
    return True


def is_generic_parent_section(title: str) -> bool:
    """Broad ## sections that should not be the public example title."""
    h = title.lower().strip()
    if header_mode(title):
        return True
    return any(
        x in h
        for x in (
            "azure devops input",
            "jenkins input",
            "jenkins inputs",
            "gitlab input",
            "circleci input",
            "travis input",
            "bamboo input",
            "bitbucket input",
            "designer pipeline",
            "jenkinsfile pipeline",
        )
    )


def resolve_example_labels(
    topic: str,
    parent_section: str,
    page_short: str,
) -> tuple[str, str]:
    """Human-readable section_title + pipeline_context slug for one example."""
    topic = (topic or "").strip()
    parent_section = (parent_section or "").strip()

    if topic and is_topic_heading(topic):
        if page_short and topic.lower() == page_short.lower():
            display = page_short
        elif is_generic_parent_section(parent_section) and page_short:
            display = f"{page_short} — {topic}"
        else:
            display = topic
    elif parent_section and not is_generic_parent_section(parent_section):
        display = parent_section
    elif page_short:
        display = page_short
    else:
        display = parent_section or page_short or "default"

    return display, slugify_pipeline_context(display)


def parse_doc_version_label(line: str) -> str | None:
    """Return normalized doc version label (V1, V2) from a standalone prose line."""
    m = DOC_VERSION_LABEL_RE.match(line.strip())
    if not m:
        return None
    return f"V{m.group(1)}"


def example_record(
    *,
    example_index: int,
    section_title: str,
    pipeline_context: str,
    variant_index: int,
    input_block: dict[str, Any] | None,
    output_block: dict[str, Any] | None,
    unsupported_options: list[str],
    example_kind: str,
    source_version: str = "",
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "example_index": example_index,
        "section_title": section_title,
        "pipeline_context": pipeline_context,
        "variant_index": variant_index,
        "input": input_block,
        "output": output_block,
        "unsupported_options": unsupported_options,
        "example_kind": example_kind,
    }
    if source_version:
        row["source_version"] = source_version
    return row


def infer_format(lang: str, content: str) -> str:
    lang = lang.lower()
    if lang in ("xml",):
        return "xml"
    if lang in ("yaml", "yml"):
        return "yaml"
    if lang in ("groovy", "jenkinsfile"):
        return "groovy"
    if lang in ("json",):
        return "json"
    if lang in ("bash", "sh", "shell"):
        return "shell"
    if content.strip().startswith("<"):
        return "xml"
    if content.strip().startswith("{"):
        return "json"
    if "steps {" in content or "sh '" in content:
        return "groovy"
    return "other"


def yaml_to_item(content: str) -> Any | None:
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError:
        return None


# Line-start hints for source CI YAML (not GitHub Actions `- name:` / `uses:` steps).
_PLATFORM_YAML_MARKERS: dict[str, tuple[str, ...]] = {
    "gitlab": (
        "trigger:",
        "include:",
        "extends:",
        "only:",
        "except:",
        "pages:",
        "stages:",
        "variables:",
        "image:",
        "services:",
        "cache:",
        "artifacts:",
        "script:",
        "before_script:",
        "after_script:",
        "environment:",
        "timeout:",
        "workflow:",
        "needs:",
        "default:",
    ),
    "travis_ci": (
        "language:",
        "dist:",
        "os:",
        "sudo:",
        "addons:",
        "git:",
        "deploy:",
        "notifications:",
        "matrix:",
        "if:",
    ),
    "bamboo": (
        "triggers:",
        "tasks:",
        "script:",
        "maven:",
    ),
}

_COMMON_YAML_MARKERS = (
    "- task:",
    "task:",
    "steps:",
    "pipe:",
    "hudson.",
    "org.jenkinsci",
    "jobs:",
    "workflow:",
)


def looks_like_source_ci(content: str, fmt: str, platform: str) -> bool:
    c = content.strip()
    if fmt == "xml":
        return True
    if fmt not in ("yaml", "yml", "other"):
        return False
    if any(m in c for m in _COMMON_YAML_MARKERS):
        return True
    if any(m in c for m in _PLATFORM_YAML_MARKERS.get(platform, ())):
        return True
    if platform == "jenkins" and ("<" in c or "builder" in c.lower()):
        return True
    # Top-level `key:` document (e.g. gitlab `trigger:\n  include:`) without ### Input header
    if re.search(r"^[a-z][a-z0-9_-]*:\s", c, re.MULTILINE) and not re.search(
        r"^\s*-\s+(?:name|uses|run):", c, re.MULTILINE
    ):
        return True
    return False


def looks_like_actions_output(content: str, fmt: str) -> bool:
    if fmt != "yaml":
        return False
    c = content.strip()
    markers = ("uses:", "runs-on:", "run:", "continue-on-error:", "env:", "with:")
    return any(m in c for m in markers)


def classify_orphan_fence(
    group_input: dict | None,
    group_outputs: list[dict],
    fmt: str,
    content: str,
    platform: str,
) -> str | None:
    """Infer input/output when a fence appears outside explicit ### headers."""
    if group_input is None and looks_like_source_ci(content, fmt, platform):
        return "input"
    if group_input is not None and not group_outputs and looks_like_actions_output(content, fmt):
        return "output"
    if group_input is None and fmt == "yaml" and looks_like_actions_output(content, fmt):
        # Rare pages with only Actions YAML (e.g. unsupported trigger samples)
        return "input"
    return None


def make_block(
    *,
    mode: str,
    content: str,
    fmt: str,
    platform: str,
    rel_posix: str,
) -> dict[str, Any]:
    execution_payloads: list[dict[str, Any]] = []
    if mode == "input":
        if platform == "circle_ci":
            execution_payloads = parse_circle_ci_payloads(content, rel_posix)
        else:
            payload = parse_input_for_platform(platform, fmt, content, rel_posix)
            if payload:
                execution_payloads = [payload]
        if not execution_payloads:
            raw_item = None
            if fmt == "xml":
                raw_item = xml_to_item_dict(content)
            elif fmt == "yaml":
                raw_item = yaml_to_item(content)
            if raw_item is not None:
                execution_payloads = [{"identifier": None, "item": raw_item}]

    block: dict[str, Any] = {
        "format": fmt,
        "content": content,
    }
    if mode == "input":
        block["execution_payloads"] = _normalize_payloads(execution_payloads)
    return block


def _normalize_payloads(payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop empty resources; omit resources key when unused."""
    out: list[dict[str, Any]] = []
    for p in payloads:
        row = dict(p)
        res = row.pop("resources", None)
        if res:
            row["resources"] = res
        out.append(row)
    return out


@dataclass
class SectionGroup:
    section_title: str
    pipeline_context: str
    topic_title: str = ""
    page_short: str = ""
    inputs: list[dict[str, Any]] = field(default_factory=list)
    input_topics: list[str] = field(default_factory=list)
    input_versions: list[str] = field(default_factory=list)
    outputs: list[dict[str, Any]] = field(default_factory=list)
    unsupported_options: list[str] = field(default_factory=list)
    last_input: dict[str, Any] | None = None

    def labels_for(self, topic: str = "", version: str = "") -> tuple[str, str]:
        sec, ctx = resolve_example_labels(
            topic or self.topic_title,
            self.section_title,
            self.page_short,
        )
        if version:
            sec = f"{sec} ({version})"
            ctx = slugify_pipeline_context(f"{ctx}_{version.lower()}")
        return sec, ctx


def example_kind(inp: dict | None, out: dict | None, unsupported: list[str]) -> str:
    if inp and out:
        return "complete"
    if inp:
        return "input_only"
    if out:
        return "output_only"
    if unsupported:
        return "unsupported_only"
    return "empty"


def _topic_for_input(group: SectionGroup, index: int) -> str:
    if index < len(group.input_topics):
        return group.input_topics[index]
    return group.topic_title


def _version_for_input(group: SectionGroup, index: int) -> str:
    if index < len(group.input_versions):
        return group.input_versions[index]
    return ""


def emit_examples(group: SectionGroup, start_index: int) -> tuple[list[dict[str, Any]], int]:
    """Turn accumulated section buffers into one or more example records."""
    if not group.inputs and not group.outputs and not group.unsupported_options:
        return [], start_index

    examples: list[dict[str, Any]] = []
    unsupported = list(group.unsupported_options)
    sec, ctx = group.labels_for()

    if group.inputs and group.outputs:
        for i, out in enumerate(group.outputs):
            inp = group.inputs[i] if i < len(group.inputs) else group.inputs[-1]
            idx = i if i < len(group.inputs) else len(group.inputs) - 1
            topic = _topic_for_input(group, idx)
            version = _version_for_input(group, idx)
            sec, ctx = group.labels_for(topic, version)
            examples.append(
                example_record(
                    example_index=start_index + len(examples),
                    section_title=sec,
                    pipeline_context=ctx,
                    variant_index=i,
                    input_block=inp,
                    output_block=out,
                    unsupported_options=unsupported,
                    example_kind=example_kind(inp, out, unsupported),
                    source_version=version,
                )
            )
    elif group.inputs:
        for i, inp in enumerate(group.inputs):
            topic = _topic_for_input(group, i)
            version = _version_for_input(group, i)
            sec, ctx = group.labels_for(topic, version)
            examples.append(
                example_record(
                    example_index=start_index + len(examples),
                    section_title=sec,
                    pipeline_context=ctx,
                    variant_index=i,
                    input_block=inp,
                    output_block=None,
                    unsupported_options=unsupported,
                    example_kind=example_kind(inp, None, unsupported),
                    source_version=version,
                )
            )
    elif group.outputs:
        for i, out in enumerate(group.outputs):
            examples.append(
                example_record(
                    example_index=start_index + len(examples),
                    section_title=sec,
                    pipeline_context=ctx,
                    variant_index=i,
                    input_block=None,
                    output_block=out,
                    unsupported_options=unsupported,
                    example_kind=example_kind(None, out, unsupported),
                )
            )
    else:
        examples.append(
            example_record(
                example_index=start_index,
                section_title=sec,
                pipeline_context=ctx,
                variant_index=0,
                input_block=None,
                output_block=None,
                unsupported_options=unsupported,
                example_kind="unsupported_only",
            )
        )

    return examples, start_index + len(examples)


def page_structure_flags(examples: list[dict[str, Any]], rel: Path) -> list[str]:
    flags: list[str] = []
    if rel.name == "index.md":
        flags.append("index_page")
    kinds = {ex.get("example_kind") for ex in examples}
    if not examples:
        flags.append("no_examples")
    if kinds == {"unsupported_only"}:
        flags.append("unsupported_only_page")
    if "complete" not in kinds and examples:
        flags.append("incomplete_pairs")
    if any(
        ex.get("input") and not input_execution_payloads(ex.get("input")) for ex in examples
    ):
        flags.append("missing_execution_payload")
    return flags


def parse_markdown_page(path: Path, docs_root: Path) -> dict[str, Any]:
    rel = path.relative_to(docs_root)
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    title = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("#") else path.stem
    page_short = page_short_name(title, rel)
    platform = detect_platform(rel)
    page_platform = platform

    examples: list[dict[str, Any]] = []
    example_index = 0

    # Page-level pairing for split ## Input / ## Transformed sections (e.g. AzurePowerShell.md)
    page_inputs: list[dict[str, Any]] = []
    page_outputs: list[dict[str, Any]] = []
    page_unsupported: list[str] = []
    split_io_page = False
    split_io_section_title = "default"
    split_io_pipeline_context = "default"

    group: SectionGroup | None = None
    mode: str | None = None
    current_topic_title = ""
    pending_input_version = ""

    # Xcode-style: #### input blocks × N then #### output blocks × N (1:1 by label or FIFO).
    # Docker-style: V1/V2 inputs under ### Login then one #### output (N:1).
    h4_input_slots = 0
    fifo_output_pairing = False
    expect_h4_input = False
    current_h4_title = ""
    input_h4_labels: list[str] = []

    in_fence = False
    fence_lang = ""
    buffer: list[str] = []

    def flush_group() -> None:
        nonlocal example_index, group, page_inputs, page_outputs, page_unsupported, split_io_page
        if group is None:
            return
        if split_io_page:
            page_inputs.extend(group.inputs)
            page_outputs.extend(group.outputs)
            page_unsupported.extend(group.unsupported_options)
        elif group.inputs or group.outputs:
            new_ex, example_index = emit_examples(group, example_index)
            examples.extend(new_ex)
        elif group.unsupported_options:
            merged = False
            for ex in reversed(examples):
                if ex.get("example_kind") == "complete":
                    existing = ex.get("unsupported_options") or []
                    ex["unsupported_options"] = existing + [
                        u for u in group.unsupported_options if u not in existing
                    ]
                    merged = True
                    break
            if not merged:
                new_ex, example_index = emit_examples(group, example_index)
                examples.extend(new_ex)
        group = None

    def flush_split_io_page() -> None:
        nonlocal example_index, page_inputs, page_outputs, page_unsupported, split_io_page, current_topic_title
        if not page_inputs and not page_outputs and not page_unsupported:
            split_io_page = False
            return
        pseudo = SectionGroup(
            section_title=split_io_section_title,
            pipeline_context=split_io_pipeline_context,
            topic_title=current_topic_title,
            page_short=page_short,
            inputs=page_inputs,
            outputs=page_outputs,
            unsupported_options=page_unsupported,
        )
        new_ex, example_index = emit_examples(pseudo, example_index)
        examples.extend(new_ex)
        page_inputs = []
        page_outputs = []
        page_unsupported = []
        split_io_page = False

    def start_group(section_title: str) -> None:
        nonlocal group, current_topic_title
        flush_group()
        topic = section_title if is_topic_heading(section_title) else ""
        current_topic_title = topic
        group = SectionGroup(
            section_title=section_title,
            pipeline_context=slugify_pipeline_context(section_title),
            topic_title=topic,
            page_short=page_short,
        )

    def _fresh_group_from(parent: SectionGroup) -> SectionGroup:
        return SectionGroup(
            section_title=parent.section_title,
            pipeline_context=parent.pipeline_context,
            topic_title=parent.topic_title,
            page_short=page_short,
        )

    def _input_topic_label() -> str:
        if current_h4_title and is_topic_heading(current_h4_title):
            return current_h4_title
        return current_topic_title or group.topic_title if group else ""

    def append_paired_examples(
        inp: dict[str, Any],
        out: dict[str, Any],
        topic: str = "",
        version: str = "",
    ) -> None:
        nonlocal example_index
        assert group is not None
        unsupported = list(group.unsupported_options)
        sec, ctx = group.labels_for(topic, version)
        examples.append(
            example_record(
                example_index=example_index,
                section_title=sec,
                pipeline_context=ctx,
                variant_index=len(examples),
                input_block=inp,
                output_block=out,
                unsupported_options=unsupported,
                example_kind="complete",
                source_version=version,
            )
        )
        example_index += 1

    def pop_input_for_h4_output() -> tuple[dict[str, Any] | None, str]:
        assert group is not None
        if not group.inputs:
            return None, ""
        target = slugify_pipeline_context(current_h4_title)
        idx = 0
        for i, lab in enumerate(input_h4_labels):
            if slugify_pipeline_context(lab) == target:
                idx = i
                break
        inp = group.inputs.pop(idx)
        if idx < len(input_h4_labels):
            input_h4_labels.pop(idx)
        version = ""
        if idx < len(group.input_topics):
            group.input_topics.pop(idx)
        if idx < len(group.input_versions):
            version = group.input_versions.pop(idx)
        return inp, version

    def append_block(block: dict[str, Any], block_mode: str) -> None:
        nonlocal group, split_io_page, h4_input_slots, expect_h4_input, pending_input_version
        if group is None:
            start_group("default")
        assert group is not None
        if block_mode == "input":
            if expect_h4_input:
                h4_input_slots += 1
                input_h4_labels.append(current_h4_title)
                expect_h4_input = False
            group.inputs.append(block)
            group.input_topics.append(_input_topic_label())
            group.input_versions.append(pending_input_version)
            pending_input_version = ""
            group.last_input = block
            return
        if block_mode == "output":
            if split_io_page:
                page_outputs.append(block)
                return
            if fifo_output_pairing and group.inputs:
                inp, version = pop_input_for_h4_output()
                if inp is not None:
                    append_paired_examples(
                        inp,
                        block,
                        current_h4_title or current_topic_title,
                        version,
                    )
                    group.last_input = inp
                return
            if group.inputs:
                topics = list(group.input_topics)
                versions = list(group.input_versions)
                for i, inp in enumerate(group.inputs):
                    topic = topics[i] if i < len(topics) else current_topic_title
                    version = versions[i] if i < len(versions) else ""
                    append_paired_examples(inp, block, topic, version)
                group.last_input = group.inputs[-1]
                group.inputs = []
                group.input_topics = []
                group.input_versions = []
                return
            if group.last_input is not None:
                append_paired_examples(
                    group.last_input,
                    block,
                    group.input_topics[-1] if group.input_topics else current_topic_title,
                    group.input_versions[-1] if group.input_versions else "",
                )
                return
            group.outputs.append(block)

    def close_open_fence() -> None:
        """Close a fence when ``` is missing (common doc typo before next ### heading)."""
        nonlocal in_fence, buffer, fence_lang
        if not in_fence:
            return
        in_fence = False
        content = "\n".join(buffer).strip()
        buffer = []
        if not content:
            return
        fmt = infer_format(fence_lang, content)
        block_mode = mode
        if block_mode is None and group is not None:
            block_mode = classify_orphan_fence(
                group.inputs[-1] if group.inputs else None,
                group.outputs,
                fmt,
                content,
                page_platform,
            )
        elif block_mode is None:
            block_mode = classify_orphan_fence(None, [], fmt, content, page_platform)
        if block_mode is None:
            return
        block = make_block(
            mode=block_mode,
            content=content,
            fmt=fmt,
            platform=page_platform,
            rel_posix=rel.as_posix(),
        )
        if block_mode == "unsupported":
            return
        append_block(block, block_mode)

    for line in lines:
        hm = HEADING_RE.match(line)
        if hm and in_fence:
            close_open_fence()
        if hm:
            level = len(hm.group(1))
            section_title = hm.group(2).strip()
            new_mode = header_mode(section_title)

            if level == 2:
                h4_input_slots = 0
                fifo_output_pairing = False
                expect_h4_input = False
                current_h4_title = ""
                input_h4_labels = []
                if is_topic_heading(section_title):
                    current_topic_title = section_title
                # Split layout: ## Platform Input (many blocks) then ## Transformed Github Action
                if new_mode == "output" and group and group.inputs:
                    split_io_page = True
                    split_io_section_title, split_io_pipeline_context = resolve_example_labels(
                        current_topic_title,
                        group.section_title,
                        page_short,
                    )
                if new_mode == "unsupported" and split_io_page and (page_inputs or group):
                    flush_group()
                    mode = "unsupported"
                    start_group(section_title)
                    continue
                if new_mode == "input" and split_io_page:
                    flush_split_io_page()
                flush_group()
                if split_io_page and new_mode == "output" and page_inputs and page_outputs:
                    flush_split_io_page()
                start_group(section_title)
                mode = new_mode
                continue

            if level == 3:
                if is_topic_heading(section_title):
                    current_topic_title = section_title
                    if group is not None:
                        group.topic_title = section_title
                        if header_mode(group.section_title) == "input":
                            mode = "input"
                if new_mode and "transformed" in section_title.lower():
                    fifo_output_pairing = h4_input_slots >= 2
                elif new_mode != "unsupported":
                    h4_input_slots = 0
                    fifo_output_pairing = False
                    input_h4_labels = []
                    current_h4_title = ""
                if new_mode == "output" and group and group.inputs and not fifo_output_pairing:
                    # Docker.md: #### Transformed under ### Login (not H3 transformed section)
                    mode = "output"
                    continue
                if new_mode == "input" and group and (group.inputs or group.last_input):
                    new_ex, example_index = emit_examples(group, example_index)
                    examples.extend(new_ex)
                    group = _fresh_group_from(group)
                if new_mode:
                    mode = new_mode
                continue

            if level == 4:
                current_h4_title = section_title
                if is_topic_heading(section_title):
                    current_topic_title = section_title
                    if group is not None:
                        group.topic_title = section_title
                if new_mode == "output":
                    expect_h4_input = False
                    if group and group.inputs and not fifo_output_pairing:
                        mode = "output"
                        continue
                elif new_mode != "unsupported":
                    expect_h4_input = True
                if new_mode == "input" and group and (group.inputs or group.last_input):
                    new_ex, example_index = emit_examples(group, example_index)
                    examples.extend(new_ex)
                    group = _fresh_group_from(group)
                if new_mode:
                    mode = new_mode
                continue

        if line.startswith("```"):
            if not in_fence:
                in_fence = True
                fence_lang = line[3:].strip()
                buffer = []
            else:
                in_fence = False
                content = "\n".join(buffer).strip()
                if not content:
                    continue
                fmt = infer_format(fence_lang, content)
                block_mode = mode
                if block_mode is None and group is not None:
                    block_mode = classify_orphan_fence(
                        group.inputs[-1] if group.inputs else None,
                        group.outputs,
                        fmt,
                        content,
                        page_platform,
                    )
                elif block_mode is None:
                    block_mode = classify_orphan_fence(None, [], fmt, content, page_platform)
                if block_mode is None:
                    continue
                block = make_block(
                    mode=block_mode,
                    content=content,
                    fmt=fmt,
                    platform=page_platform,
                    rel_posix=rel.as_posix(),
                )
                if block_mode == "unsupported":
                    continue
                append_block(block, block_mode)
            continue

        if in_fence:
            buffer.append(line)
            continue

        version_label = parse_doc_version_label(line)
        if version_label and group is not None and mode != "unsupported":
            pending_input_version = version_label
            continue

        if mode == "unsupported" and line.strip() and group is not None:
            cleaned = line.strip().lstrip("- ").strip()
            if cleaned and not cleaned.startswith("#") and cleaned.lower() not in (
                "none",
                "n/a",
                "na",
            ):
                group.unsupported_options.append(cleaned)

    close_open_fence()
    flush_group()
    flush_split_io_page()

    # Re-number example_index sequentially
    for i, ex in enumerate(examples):
        ex["example_index"] = i

    return {
        "relative_path": rel.as_posix(),
        "platform": platform,
        "title": title,
        "page_kind": _page_kind(rel),
        "structure_flags": page_structure_flags(examples, rel),
        "examples": examples,
    }


def _page_kind(rel: Path) -> str:
    if rel.name == "index.md":
        return "index"
    if "Orbs" in rel.parts or "orbs" in rel.as_posix():
        return "orb"
    if "pipes" in rel.parts:
        return "pipe"
    if "plugins" in rel.parts:
        return "plugin"
    if "Steps" in rel.parts:
        return "step"
    if "triggers" in rel.parts:
        return "trigger"
    return "other"


def audit_corpus(data: dict[str, Any]) -> dict[str, Any]:
    from collections import Counter

    kinds = Counter()
    missing_payload = 0
    for page in data.get("pages", []):
        for ex in page.get("examples", []):
            kinds[ex.get("example_kind", "unknown")] += 1
            inp = ex.get("input")
            if inp and not input_execution_payloads(inp) and (inp.get("content") or "").strip():
                missing_payload += 1

    return {
        "page_count": data.get("page_count"),
        "example_count": sum(len(p.get("examples", [])) for p in data.get("pages", [])),
        "example_kinds": dict(kinds),
        "inputs_missing_execution_payload": missing_payload,
        "pages_with_incomplete_pairs": sum(
            1 for p in data.get("pages", []) if "incomplete_pairs" in p.get("structure_flags", [])
        ),
    }


def build_corpus(docs_path: Path) -> dict[str, Any]:
    pages = []
    for md in sorted(docs_path.rglob("*.md")):
        if md.name == "index.md" and md.parent == docs_path:
            continue
        pages.append(parse_markdown_page(md, docs_path))
    return {
        "schema_version": 1,
        "source": "gh-actions-importer/docs",
        "page_count": len(pages),
        "pages": pages,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build docs JSON from Markdown")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--force", action="store_true", help="Overwrite existing extracted JSON")
    parser.add_argument("--audit", action="store_true", help="Print completeness audit after build")
    args = parser.parse_args()

    cfg = Config.load(args.config)
    if cfg.extracted_json.exists() and not args.force:
        print(f"Extracted JSON exists: {cfg.extracted_json} (use --force to regenerate)")
        return

    data = build_corpus(cfg.docs_path)
    audit = audit_corpus(data)
    data["build_audit"] = audit
    write_json(cfg.extracted_json, data)
    print(f"Wrote {cfg.extracted_json} ({data['page_count']} pages)")
    print(
        f"  examples: {audit['example_count']} | complete: {audit['example_kinds'].get('complete', 0)} | "
        f"input_only: {audit['example_kinds'].get('input_only', 0)} | "
        f"output_only: {audit['example_kinds'].get('output_only', 0)} | "
        f"unsupported_only: {audit['example_kinds'].get('unsupported_only', 0)}"
    )
    if args.audit:
        print(f"  audit: {audit}")


if __name__ == "__main__":
    main()
