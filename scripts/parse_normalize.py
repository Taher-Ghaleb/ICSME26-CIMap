"""Parse-oriented repairs for doc fenced snippets (documentation fragments)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

STEP_CHILD_KEYS = frozenset(
    {"with", "env", "shell", "run", "if", "name", "uses", "working-directory", "continue-on-error"}
)


@dataclass
class RepairResult:
    text: str
    kinds: list[str] = field(default_factory=list)
    changed: bool = False

    def note(self, kind: str) -> None:
        if kind not in self.kinds:
            self.kinds.append(kind)
        self.changed = True


def _nl(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def repair_yaml_input(text: str, platform_slug: str) -> RepairResult:
    res = RepairResult(text=_nl(text))
    lines = res.text.split("\n")
    out: list[str] = []

    for i, line in enumerate(lines):
        if re.match(r"^\s*task:", line) and not re.match(r"^\s*-\s*task:", line):
            line = "- " + line.lstrip()
            res.note("input_task_list_prefix")
        out.append(line)

    # CircleCI: "- attach_workspace" without colon before nested keys
    fixed: list[str] = []
    for i, line in enumerate(out):
        m = re.match(r"^(\s*)- (\S+)$", line)
        if m and i + 1 < len(out):
            nxt = out[i + 1]
            if re.match(r"^\s+\w", nxt) and ":" in nxt.split("#", 1)[0]:
                line = f"{m.group(1)}- {m.group(2)}:"
                res.note("input_step_key_colon")
        fixed.append(line)
    out = fixed

    # Bare "variables:" under bitbucket pipes (missing pipe header)
    if platform_slug == "bitbucket" and out and out[0].strip().startswith("variables:"):
        out = ["pipe: unknown"] + out
        res.note("input_bitbucket_pipe_prefix")

    # GitLab/Circle single mapping under steps: without document root
    stripped = "\n".join(out).strip()
    if stripped and not parse_yaml_quick(stripped):
        if out and out[0].strip() == "steps:":
            pass  # keep
        elif any(re.match(r"^\s*\w[\w-]*:\s*", ln) for ln in out if ln.strip()):
            if not stripped.startswith("steps:") and platform_slug in ("circle_ci", "gitlab"):
                out = ["steps:"] + out
                res.note("input_steps_root")

    res.text = "\n".join(out)
    return res


def repair_yaml_output(text: str) -> RepairResult:
    res = RepairResult(text=_nl(text))
    lines = res.text.split("\n")

    # Single-step dict without list marker (name/shell/run/uses at column 0)
    non_comment = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    if non_comment and not any(ln.lstrip().startswith("-") for ln in non_comment):
        first = non_comment[0].split(":", 1)[0].strip()
        if first in ("name", "shell", "run", "uses", "env", "with", "if"):
            body = res.text.strip("\n")
            res.text = "- " + body.replace("\n", "\n  ", 1) if "\n" in body else f"- {body}"
            # Re-expand: wrap as one list item with indented continuation
            inner_lines = body.split("\n")
            rebuilt = ["- " + inner_lines[0]]
            for ln in inner_lines[1:]:
                rebuilt.append("  " + ln)
            res.text = "\n".join(rebuilt)
            res.note("output_wrap_single_step")
            lines = res.text.split("\n")

    # Ensure first actionable line has list prefix when it looks like a step
    if lines:
        for i, line in enumerate(lines):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if not s.startswith("-") and s.split(":", 1)[0] in ("uses", "name", "run", "shell"):
                lines[i] = "- " + line.lstrip()
                res.note("output_step_list_prefix")
            break

    # Dedent over-indented step children (doc typo: "      with:" under "- uses:")
    repaired: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^(\s*)- ", line)
        if m:
            base = len(m.group(1))
            repaired.append(line)
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if not nxt.strip() or nxt.strip().startswith("#"):
                    repaired.append(nxt)
                    i += 1
                    continue
                m_sibling = re.match(r"^(\s*)- ", nxt)
                if m_sibling and len(m_sibling.group(1)) <= base:
                    break
                m_key = re.match(r"^(\s+)([\w-]+):", nxt)
                if m_key:
                    key = m_key.group(2)
                    indent_len = len(m_key.group(1))
                    expected = base + 2
                    if indent_len > expected and key in STEP_CHILD_KEYS:
                        shift = indent_len - expected
                        block: list[str] = []
                        j = i
                        while j < len(lines):
                            cur = lines[j]
                            if j > i and cur.strip() and not cur.strip().startswith("#"):
                                cur_indent = len(cur) - len(cur.lstrip())
                                if cur_indent <= indent_len - 1 and re.match(
                                    r"^\s*-\s", cur
                                ):
                                    break
                                if cur_indent <= expected and re.match(
                                    r"^\s+[\w-]+:", cur
                                ):
                                    mk = re.match(r"^(\s+)([\w-]+):", cur)
                                    if mk and mk.group(2) in STEP_CHILD_KEYS:
                                        break
                            if j > i and cur.strip() and len(cur) - len(cur.lstrip()) <= expected:
                                if re.match(r"^\s*-\s", cur):
                                    break
                            block.append(cur)
                            j += 1
                        for bi, cur in enumerate(block):
                            if not cur.strip() or cur.strip().startswith("#"):
                                repaired.append(cur)
                                continue
                            cur_indent = len(cur) - len(cur.lstrip())
                            if cur_indent >= shift:
                                repaired.append(" " * (cur_indent - shift) + cur.lstrip())
                            else:
                                repaired.append(cur)
                            if bi == 0:
                                res.note("output_step_dedent")
                        i = j
                        continue
                repaired.append(nxt)
                i += 1
        else:
            repaired.append(line)
            i += 1

    res.text = "\n".join(repaired)
    return res


def repair_xml_input(text: str) -> RepairResult:
    res = RepairResult(text=_nl(text).strip())
    raw = res.text
    if not raw or raw.startswith("{"):
        return res

    # Try lxml recover
    try:
        from lxml import etree  # type: ignore

        parser = etree.XMLParser(recover=True, encoding="utf-8")
        root = etree.fromstring(raw.encode("utf-8"), parser=parser)
        res.text = etree.tostring(root, encoding="unicode")
        res.note("xml_lxml_recover")
        return res
    except ImportError:
        pass
    except Exception:
        pass

    # Multi-root / junk after first element: keep first top-level XML element
    start = raw.find("<")
    if start >= 0:
        fragment = raw[start:]
        # Heuristic: first closing tag at depth 0 after first open
        open_m = re.match(r"^<([\w.:$-]+)", fragment)
        if open_m:
            tag = re.escape(open_m.group(1))
            close_pat = re.compile(rf"</{tag}\s*>", re.IGNORECASE)
            m_close = close_pat.search(fragment)
            if m_close:
                candidate = fragment[: m_close.end()]
                if parse_xml_quick(candidate):
                    res.text = candidate
                    res.note("xml_first_element")
                    return res

    # Strip BOM / leading junk before <
    if start > 0:
        trimmed = raw[start:]
        if parse_xml_quick(trimmed):
            res.text = trimmed
            res.note("xml_trim_leading")

    return res


def parse_yaml_quick(text: str) -> bool:
    import yaml

    try:
        list(yaml.safe_load_all(text))
        return True
    except yaml.YAMLError:
        return False


def parse_xml_quick(text: str) -> bool:
    import xml.etree.ElementTree as ET

    try:
        ET.fromstring(text)
        return True
    except ET.ParseError:
        return False


def repair_for_parse(
    text: str,
    fmt: str,
    side: str,
    platform_slug: str,
) -> RepairResult:
    fmt = (fmt or "other").lower()
    if fmt == "yaml":
        if side == "input":
            return repair_yaml_input(text, platform_slug)
        return repair_yaml_output(text)
    if fmt == "xml" and side == "input":
        return repair_xml_input(text)
    return RepairResult(text=_nl(text))
